"""Comprehensive UI action handling and coordination system.

This module serves as the central coordinator for all direct user interface
interactions within the WheelHouse system. It delegates to specialist classes
for focused responsibilities while maintaining the public API expected by
input_proc.py.

Key Classes:
  - UIActionHandler: Main coordinator that delegates to specialists

Integration Components:
  - TextPerfector: Text formatting logic
  - ClipboardOperations: Clipboard interactions
  - WindowFocusManager: Window focus management
  - SelectionTransformer: Case conversion and wrapping
  - UtteranceClipboardManager: Utterance lifecycle
  - ShadowBufferManager: Cached UIA state
  - TerminalEditorProxy: IPC proxy for terminal editor in GUI Process
  - InsertionStrategies: Different insertion approaches

Supported Operations:
  - Text insertion with intelligent context detection
  - Selection transformations (wrapping and case conversion)
  - Hotkey execution and key presses
  - Utterance lifecycle management
  - Buffer invalidation for user input
"""
import logging
import time
import uuid
import pyperclip
import win32gui
import win32con
import win32process
from multiprocessing import Queue
from typing import TYPE_CHECKING, Any, NamedTuple, Optional
from utils.win_input_sender import (
    VK_CODE_MAP,
    WHEEL_REFUSALS_THAT_LOG_ERROR,
    _send_modifier_keyups,
    press_keys,
    send_backspaces,
    type_string,
    type_string_verified,
    verified_press_keys,
)
from utils.clipboard_manager import clipboard_context
from utils.logging_setup import get_notifier_worker
from utils.redact import redact_transcript

from .text_perfector import TextPerfector
from .clipboard_operations import ClipboardOperations
from .window_focus_manager import WindowFocusManager
from .selection_transformer import SelectionTransformer
from .utterance_clipboard_manager import UtteranceClipboardManager
from .shadow_buffer import ShadowBufferManager
from .terminal_editor_proxy import TerminalEditorProxy
from .response_handler import ResponseHandler
from .continuous_scroll import (
    NOTICE_TITLE,
    ContinuousScroller,
    send_notice,
)
from . import settle_detector
from .hwnd_utils import (
    normalize_hwnd_for_foreground_compare,
    read_hwnd_provenance,
    resolve_same_process_browser_names,
    top_level_hwnd_from_control,
)

if TYPE_CHECKING:  # pragma: no cover -- typing only, no runtime import cost
    from ui.element_types import ClickGesture

# New Router/Strategy Components
from .context import capture_context
from .phrase_select_notice import notify_outcome
from .uia_phrase_select import (
    PHRASE_FAILED,
    read_focused_control,
    select_phrase_in_control,
)
from .elevation_check import target_elevation_state
from .router import InsertionRouter
from .strategies.base import InsertionMode, InsertionOptions
from .strategies.specific import (
    _context_hwnd,
    ClipboardOnlyStrategy,
    StandardStrategy,
    FlutterStrategy,
    SimplePasteStrategy,
    UnicodeFirstStrategy,
    VerifiedUnicodeStrategy,
    RejectedInsertionStrategy,
)
from .text_target import build_predicate_from_config


from speech.text_transforms import auto_compress_spelled_letters

logger = logging.getLogger(__name__)

# Transient stale-window UIA/COM error classes (wh-overlay-walk-theme-error).
# Same guarded pair as ui/uia_walker.py and ui/element_finder.py duplicate
# (the base-package comtypes import does not trigger type-library generation;
# OSError covers Win32/ctypes failures and hosts without comtypes). A walk can
# raise these when the focused window is rebuilding -- a Windows theme switch
# recreates the shell window and every walker retry can land inside the
# rebuild, surfacing COMError -2147220991 ("An event was unable to invoke any
# of the subscribers"). The walk-running handlers log this class at WARNING
# and map it to a normal execution_failed: it self-heals on the next
# focus-hook re-walk, and an ERROR record would pop a Windows notification
# box (utils/error_notifier.py) in the user's face for a blip.
try:
    from comtypes import COMError as _COMError  # type: ignore[import-not-found]

    _TRANSIENT_WALK_ERRORS: tuple[type[BaseException], ...] = (OSError, _COMError)
except Exception:  # noqa: BLE001 -- no comtypes -> OSError covers the test fakes
    _TRANSIENT_WALK_ERRORS = (OSError,)


class PasteFailedError(RuntimeError):
    """Raised by raw_insert_text when the underlying strategy reports failure.

    raw_insert_text is not in input_proc._HANDLES_OWN_RESPONSE; the generic
    dispatcher emits a heuristic "ok" response after a fixed delay if the
    handler returns normally. Raising this exception drives the dispatcher's
    except branch instead, which produces a Schema A error response when a
    request_id is set (and a logged-only failure for fire-and-forget callers).

    wh-fsov0: replaces the prior pattern where verified_paste returning False
    was silently ignored, leaving the caller's Future to resolve with a
    heuristic success while the dictated text leaked onto the clipboard.
    """


# ---------------------------------------------------------------------------
# Voice-clicking production seams (wh-tab7j).
#
# ElementFinder and ClickExecutor inject every Win32 / display seam as a
# constructor callable so the test suite runs headless. These module-level
# functions are the real-Win32 implementations the Input process wires in.
# Each is fail-soft: a degraded host falls back to a safe value rather than
# crashing the click path. Tests never reach these -- they construct the
# coordinator/executor with their own fakes.
# ---------------------------------------------------------------------------


def _win32_dpi_resolver(monitor_id: int) -> float:
    """Real per-monitor effective DPI for an HMONITOR-as-int. Falls back to 96.0.

    The ``monitor_id`` is an HMONITOR cast to int, produced by the sibling
    :func:`_win32_monitor_resolver` (or 0 on a degraded host). This resolver
    calls shcore ``GetDpiForMonitor`` with ``MDT_EFFECTIVE_DPI`` so the
    clear-winner cursor-proximity tiebreaker scales its influence radius at
    the true per-monitor scale (e.g. 144/192 DPI), not always 96. The Input
    process is already per-monitor-DPI aware (uiautomation calls
    SetProcessDpiAwareness at import), so the effective DPI returned here is
    the monitor's real scaling, not the system DPI.

    Fail-soft: on a non-zero HRESULT (shcore unavailable, monitor_id 0, or a
    stale HMONITOR), a degenerate (<= 0) DPI value, or any exception, returns
    96.0 (100% scaling). The consumer (clear_winner_rule.decide) abstains to
    an 'ambiguous' outcome if this returns a non-finite or non-positive value,
    so this function must never raise and must return a positive finite float.
    """
    MDT_EFFECTIVE_DPI = 0
    try:
        import ctypes
        from ctypes import wintypes

        # Declare argtypes/restype so ctypes marshals the HMONITOR at the
        # correct (pointer) width even if the explicit HANDLE() cast below is
        # ever removed (wh-9f3t.80.1). Without this, a future change passing a
        # bare Python int would fall back to ctypes' default c_int conversion,
        # which truncates the upper 32 bits of the handle on x64 and could
        # silently read a different monitor's DPI. The HANDLE() cast already
        # makes today's call correct; this is defense in depth.
        gdfm = ctypes.windll.shcore.GetDpiForMonitor
        gdfm.argtypes = [
            wintypes.HANDLE,                  # HMONITOR
            wintypes.UINT,                    # MONITOR_DPI_TYPE
            ctypes.POINTER(wintypes.UINT),    # UINT* dpiX
            ctypes.POINTER(wintypes.UINT),    # UINT* dpiY
        ]
        gdfm.restype = wintypes.LONG          # HRESULT

        dpi_x = wintypes.UINT(0)
        dpi_y = wintypes.UINT(0)
        hr = gdfm(
            wintypes.HANDLE(monitor_id),
            MDT_EFFECTIVE_DPI,
            ctypes.byref(dpi_x),
            ctypes.byref(dpi_y),
        )
        if hr != 0 or dpi_x.value <= 0:
            return 96.0
        return float(dpi_x.value)
    except Exception:  # noqa: BLE001 -- fail soft on a degraded host
        return 96.0


def _win32_monitor_resolver(bounds: tuple) -> int:
    """Resolve a (x, y, w, h) physical box to a stable monitor id.

    Uses ``MonitorFromPoint`` on the box centre; the returned HMONITOR is a
    stable per-session handle that serves as the monitor id for the
    cross-monitor gate (cursor and candidates share this one namespace).
    Falls back to 0 on any failure so a degraded host treats every control
    as on the same monitor (the gate then never abstains spuriously).
    """
    try:
        import win32api
        import win32con

        x, y, w, h = bounds
        cx = int(x) + int(w) // 2
        cy = int(y) + int(h) // 2
        hmon = win32api.MonitorFromPoint(
            (cx, cy), win32con.MONITOR_DEFAULTTONEAREST,
        )
        return int(hmon)
    except Exception:  # noqa: BLE001 -- fail soft on a degraded host
        return 0


def _win32_on_screen(x: int, y: int) -> bool:
    """True when the physical point is on a visible monitor.

    ``MonitorFromPoint`` with ``MONITOR_DEFAULTTONULL`` returns a null
    handle when the point is off every monitor. On any failure we fail
    closed (return False) so a bounds re-read we cannot verify is treated
    as off-screen -- the executor then reports ``target_moved_offscreen``
    rather than clicking an unverifiable coordinate.
    """
    try:
        import win32api
        import win32con

        hmon = win32api.MonitorFromPoint(
            (int(x), int(y)), win32con.MONITOR_DEFAULTTONULL,
        )
        return bool(hmon)
    except Exception:  # noqa: BLE001 -- fail closed
        return False


def _win32_coordinate_click(x: int, y: int) -> tuple[bool, int, str | None]:
    """SendInput-backed coordinate click for the executor's fallback seam.

    Wires the real :func:`utils.win_input_sender.click_at` into
    ``ClickExecutor`` (wh-l4h.1). ``click_at`` is itself fail-closed (it
    verifies the cursor landed before synthesising any button event and
    returns ``(False, 0, None)`` on a wrong landing) and fail-soft (any
    internal Win32/ctypes error returns ``(False, 0, None)``). This wrapper
    adds defence in depth so an import or call error at the seam boundary
    cannot escape into the click path -- it mirrors the sibling ``_win32_*``
    seams. The executor also catches a raising seam, so this is
    belt-and-braces.

    Returns ``(succeeded, events_sent, reason)`` where ``events_sent`` counts
    only the LEFTDOWN/LEFTUP pair, so the executor's ``events_sent < 2``
    short-send check stays meaningful, and ``reason`` is ``"release_failed"``
    when a partial batch's compensating release was refused too
    (wh-mouse-grid.1.5).
    """
    try:
        from utils.win_input_sender import click_at

        return click_at(int(x), int(y))
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        return (False, 0, None)


def _win32_click_point(
    x, y, button: str, click_count: int,
) -> tuple[bool, str | None]:
    """SendInput-backed grid click for the ``click_point`` handler's seam.

    Wires the real :func:`utils.win_input_sender.click_point`
    (wh-input-mouse-primitives). The primitive validates its own arguments and
    never raises, so the arguments are passed through UNCONVERTED -- coercing
    them here would turn a malformed IPC value into a raise instead of the
    ``invalid_point`` / ``invalid_button`` / ``invalid_click_count`` refusal
    the primitive reports. The try/except is defence in depth for an import
    failure on a degraded host, mirroring the sibling ``_win32_*`` seams.
    """
    try:
        from utils.win_input_sender import click_point

        return click_point(x, y, button=button, click_count=click_count)
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        logger.error("_win32_click_point: seam failed", exc_info=True)
        return (False, "sendinput_error")


def _win32_move_pointer(x, y) -> tuple[bool, str | None]:
    """SendInput-backed pointer park for the ``move_pointer`` handler's seam.

    Wires the real :func:`utils.win_input_sender.move_pointer_to`. Same
    pass-through and fail-soft contract as :func:`_win32_click_point`.
    """
    try:
        from utils.win_input_sender import move_pointer_to

        return move_pointer_to(x, y)
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        logger.error("_win32_move_pointer: seam failed", exc_info=True)
        return (False, "sendinput_error")


def _win32_scroll_wheel(direction, clicks) -> tuple[bool, str | None]:
    """SendInput-backed wheel turn for the ``scroll_wheel`` handler's seam.

    Wires the real :func:`utils.win_input_sender.scroll_wheel`
    (wh-voice-access-parity.2.3). Same pass-through contract as
    :func:`_win32_click_point`: the primitive validates its own arguments and
    reports ``invalid_direction`` / ``invalid_clicks`` rather than raising, so
    nothing is coerced here.

    The fail-soft record is where this seam DIFFERS from its siblings -- it
    is a WARNING, for the reason the except branch below states.
    """
    try:
        from utils.win_input_sender import scroll_wheel

        return scroll_wheel(direction, clicks)
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        # WARNING, not ERROR, and this seam alone among the ``_win32_*``
        # seams (wh-wheel-refusal-notice.1.3). Both wheel callers write
        # their own notice for a reason outside
        # ``WHEEL_REFUSALS_THAT_LOG_ERROR``, and "sendinput_error" is
        # outside it: the discrete scroll calls
        # ``_say_the_wheel_would_not_turn``, and the continuous scroller
        # stops with ``WHEEL_FAILED_MESSAGE``. An ERROR record here adds
        # the generic ErrorNotificationHandler box beside that notice,
        # which is the duplicate this bead removes.
        #
        # The sibling seams (_win32_click_point, _win32_move_pointer,
        # _win32_drag_pointer) keep their ERROR, and NOT because it is
        # right. An earlier version of this comment said their callers
        # never report to the user; that was false, and correcting it is
        # wh-wheel-refusal-notice.1.4. Their handlers answer with a
        # MouseActionResponse, and main.py::_send_mouse_action turns
        # every non-ok response into _forward_click_notice, so a refused
        # grid click, move or drag ALREADY shows an action-specific
        # notice -- and the ERROR record adds the generic box beside it.
        # That is the same duplicate this bead removes for the wheel.
        #
        # It is left alone here on purpose: the pointer paths are the
        # mouse-grid subsystem, this bead's acceptance criteria are the
        # wheel, and the change reaches code no test on this branch
        # exercises. It was escalated to the product owner rather than
        # fixed or filed, because pre-existing behaviour is his to
        # classify. Do NOT read this comment as saying the pointer
        # records are correct.
        logger.warning("_win32_scroll_wheel: seam failed", exc_info=True)
        return (False, "sendinput_error")


def _win32_drag_pointer(
    start_x, start_y, end_x, end_y, duration_ms,
) -> tuple[bool, str | None]:
    """SendInput-backed drag for the ``perform_drag`` handler's seam.

    Wires the real :func:`utils.win_input_sender.drag_pointer`, which owns the
    whole gesture (button down, interpolated movement, guaranteed release) so
    no IPC boundary sits between the press and the release. Same pass-through
    and fail-soft contract as :func:`_win32_click_point`.
    """
    try:
        from utils.win_input_sender import drag_pointer

        return drag_pointer(
            start_x, start_y, end_x, end_y, duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        logger.error("_win32_drag_pointer: seam failed", exc_info=True)
        return (False, "sendinput_error")


def _parse_gesture(value: object) -> "ClickGesture":
    """Map a request's gesture field onto a ClickGesture, never raising.

    The numbered-badge request carries the gesture as a bare payload value
    (wh-click-gesture-param), so it arrives as whatever Logic put there --
    including nothing at all from an older Logic build. Anything unrecognised
    degrades to the DEFAULT gesture: today's Invoke behaviour, which sends no
    synthetic mouse input. Guessing a physical gesture from a malformed value
    is the one outcome this must never produce.
    """
    from ui.element_types import DEFAULT_GESTURE, ClickGesture

    if not value:
        return DEFAULT_GESTURE
    try:
        return ClickGesture(value)
    except (TypeError, ValueError):
        logger.warning(
            "click_snapshot_item: unrecognised gesture %r; using the default "
            "Invoke behaviour", value,
        )
        return DEFAULT_GESTURE


def _win32_gesture_click(
    x: int, y: int, button: str, click_count: int
) -> tuple[bool, int, str | None]:
    """SendInput-backed gesture click for the executor's gesture seam.

    The wh-click-gesture-param sibling of :func:`_win32_coordinate_click`: same
    fail-soft wrapper, same underlying :func:`utils.win_input_sender.click_at`,
    but carrying the spoken gesture's button and click count. It is a SEPARATE
    seam so the default gesture keeps calling the two-argument seam with an
    unchanged argument list.

    ``click_at`` fails closed on an unknown button or a non-positive count
    (``(False, 0, None)``, no input sent), so a malformed gesture cannot reach
    SendInput. Returns ``(succeeded, events_sent, reason)`` where
    ``events_sent`` counts two events per click (what the executor's
    short-send check expects) and ``reason`` is ``"release_failed"`` when a
    partial batch's compensating release was refused too
    (wh-mouse-grid.1.5).
    """
    try:
        from utils.win_input_sender import click_at

        return click_at(
            int(x), int(y), button=button, click_count=int(click_count)
        )
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        return (False, 0, None)


def _win32_root_window_at_point(x: int, y: int) -> int:
    """Click-point hit-test for the executor's coordinate fallback seam.

    Wires the real :func:`utils.win_input_sender.root_window_at_point`
    (``WindowFromPoint`` -> ``GetAncestor(GA_ROOT)``) into ``ClickExecutor``
    (wh-explorer-navpane-click.1.1). Unlike the sibling seams this one lets a
    raise PROPAGATE on purpose: the executor maps any hit-test failure to a
    fail-closed ``click_point_obstructed`` refusal, and converting a raise to
    a fabricated window handle here (0 or otherwise) would just relabel the
    same refusal while hiding the real error from the log.
    """
    from utils.win_input_sender import root_window_at_point

    return root_window_at_point(int(x), int(y))


def _uia_point_hits_winner(automation, winner, x: int, y: int) -> bool:
    """UIA point-hits-winner check for the executor's coordinate fallback.

    Wires the real :func:`ui.uia_walker.point_hits_winner`
    (``ElementFromPoint`` plus a bounded bidirectional ancestor comparison
    against the winner's live COM element) into ``ClickExecutor``
    (wh-explorer-navpane-click.1.4). Raises PROPAGATE on purpose, exactly
    like ``_win32_root_window_at_point`` above: the executor maps any raise
    to a fail-closed ``click_point_obstructed`` refusal.
    """
    from ui import uia_walker

    return uia_walker.point_hits_winner(
        automation, winner.control_ref, int(x), int(y)
    )


# Session-level sentinel for a permanently-failed IUIAutomation root
# (wh-n29v.72.2). create_automation() RAISES (never returns None) on a degraded
# UIA host (locked-down / headless / broken UIAutomationCore). Storing THIS
# sentinel on self._click_automation_root after the first failure lets
# _get_click_element_finder short-circuit on every later click WITHOUT
# re-running ClickConfig.from_raw + a failing CoCreateInstance + exception
# unwind on the latency-sensitive command-reader loop -- the per-utterance
# retry storm the finding describes. It is distinct from None (root not yet
# built) and from a real root object.
_AUTOMATION_UNAVAILABLE = object()

# wh-wheel-refusal-notice: what a refused DISCRETE spoken scroll tells the
# user. David chose these words (QUESTIONS-2026-08-30 item 8). He wrote
# them lowercase; they ship capitalized because a screen reader reads the
# notice out as a sentence. The continuous scroll keeps its own separate
# wording.
SCROLL_REFUSED_MESSAGE = "Scrolling failed"

# wh-keyboard-refusal-notice. What the user reads when a spoken key press,
# hotkey or typing action did not reach the target application. Each names
# the action and the cause, because "something went wrong" tells a hands-free
# user nothing about what to try next.
#
# The typed text NEVER appears in TYPING_REFUSED_MESSAGE. The counts are the
# report: the text is the user's own dictated content, and a notice sits on
# screen where anyone in the room can read it.
PRESS_KEY_UNKNOWN_MESSAGE = (
    "Cannot press {keys}: Wheelhouse does not know that key name"
)
PRESS_KEY_REFUSED_MESSAGE = "Pressing {keys} did not work"
HOTKEY_UNKNOWN_MESSAGE = (
    "Cannot run the {chord} shortcut: Wheelhouse does not know {keys}"
)
HOTKEY_REFUSED_MESSAGE = "The {chord} shortcut did not work"
TYPING_REFUSED_MESSAGE = "Typing stopped after {sent} of {total} characters"

# The wheel refusals that report themselves through the generic [ERROR]
# box, and so must NOT get a written notice as well. It is EMPTY, and the
# reason it is empty is worth keeping (wh-wheel-refusal-notice.1.7): the
# box comes from ErrorNotificationHandler, which drops a repeat of the
# same (logger, level, message) inside ten seconds, so an ERROR record is
# not proof the user was told anything. An earlier version of this
# comment said a listed reason "already shows the generic [ERROR] box"
# and could therefore be left silent here; that reasoning cost the user
# every notice for a repeated refusal. No wheel refusal logs an ERROR any
# more, and every one of them is reported by the caller's own written
# notice.
#
# The primitive owns the list, and this module reads it rather than repeating
# it (wh-wheel-refusal-notice.1.1). The decision is about the primitive's own
# log level, and a hand-copied list here could drift: a refusal reason added
# later could log at ERROR over there and never reach a copy over here, which
# brings the duplicate box straight back.
_WHEEL_VALIDATION_REFUSALS = WHEEL_REFUSALS_THAT_LOG_ERROR


def _win32_foreground_probe():
    """Sample the CURRENT foreground identity for pre-click verification.

    Returns a ``ForegroundProbe``. ``window`` is always obtainable
    (GetForegroundWindow needs no process handle). ``pid`` /
    ``process_name`` / ``window_creation_time`` are set to ``None`` when a
    read is denied (admin-elevated foreground), which drives the
    executor's graceful-degrade rule.
    """
    from ui.click_executor import ForegroundProbe

    window = 0
    pid: Optional[int] = None
    process_name: Optional[str] = None
    creation_time: Optional[int] = None
    try:
        window = int(win32gui.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        window = 0
    try:
        # GetWindowThreadProcessId(0) does NOT fail -- it returns the PID of
        # the thread that owns the calling process's desktop window (i.e.
        # WheelHouse's own PID). Guard on a real window so a failed
        # GetForegroundWindow reaches the pid=None fail-soft sentinel instead
        # of falsely reporting WheelHouse as the foreground (wh-9f3t.56.1).
        if window:
            _thread, raw_pid = win32process.GetWindowThreadProcessId(window)
            pid = int(raw_pid) if raw_pid else None
    except Exception:  # noqa: BLE001
        pid = None
    if pid:
        try:
            import psutil

            proc = psutil.Process(pid)
            process_name = proc.name()
            creation_time = int(proc.create_time() * 1000)
        except Exception:  # noqa: BLE001 -- access denied / gone
            process_name = None
            creation_time = None
    return ForegroundProbe(
        window=window,
        pid=pid,
        process_name=process_name,
        window_creation_time=creation_time,
    )


def _capture_click_foreground():
    """Snapshot the foreground identity + cursor BEFORE a click walk.

    Returns a ``ForegroundContext`` the ElementFinder walks against. Read
    failures degrade to safe sentinels (0 / "" / (0, 0)) so the walk still
    runs; the executor's pre-click verification re-reads and fails closed
    if the identity cannot be confirmed at click time.
    """
    from ui.element_finder import ForegroundContext

    window = 0
    pid = 0
    process_name = ""
    creation_time = 0
    cursor = (0, 0)
    try:
        window = int(win32gui.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        window = 0
    try:
        # GetWindowThreadProcessId(0) returns WheelHouse's own PID rather than
        # failing, so guard on a real window; a failed GetForegroundWindow
        # then reaches the pid=0 sentinel and the executor's pre-click
        # verification fails closed instead of capturing a wrong identity
        # (wh-9f3t.56.1).
        if window:
            _thread, raw_pid = win32process.GetWindowThreadProcessId(window)
            pid = int(raw_pid) if raw_pid else 0
    except Exception:  # noqa: BLE001
        pid = 0
    if pid:
        try:
            import psutil

            proc = psutil.Process(pid)
            process_name = proc.name()
            creation_time = int(proc.create_time() * 1000)
        except Exception:  # noqa: BLE001
            process_name = ""
            creation_time = 0
    try:
        cursor = win32gui.GetCursorPos()
    except Exception:  # noqa: BLE001
        cursor = (0, 0)
    return ForegroundContext(
        foreground_window=window,
        foreground_pid=pid,
        foreground_process_name=process_name,
        foreground_window_creation_time=creation_time,
        cursor_at_walk=(int(cursor[0]), int(cursor[1])),
        cursor_monitor_id=0,
    )


def _stop_command_on_failed_focus_read(context, action_name: str) -> None:
    """Re-raise a failed focused-control read for the COMMAND actions.

    wh-insert-focus-read-stall.1.4 (codex round 3). Before this branch
    ``capture_context`` let the COM error from the focused-control read
    escape, and each command action's own ``except Exception`` arm caught
    it before any key was sent. The read no longer raises, so without
    this the four command paths carry on with a control of None and send
    Ctrl+C, a key, or a chord into whatever window then holds the
    foreground -- usually the window the person just moved to, because a
    stalled read is what gave them time to move.

    Re-raising the ORIGINAL exception, rather than refusing here, is what
    makes the outcome identical to the pre-branch one: the same except
    arm runs, at the same log level, and formats the same error. The
    level differs between the actions on purpose.
    ``transform_selection`` and ``wrap_or_insert`` log at ERROR, which
    ErrorNotificationHandler turns into a Windows notice;
    ``press_key_action`` and ``hotkey_action`` log at WARNING and show
    nothing.

    The dictation INSERTION paths must NOT call this. Pasting the word
    into the unchanged window is the purpose of
    wh-insert-focus-read-stall, so ``_execute_insert_with_ack`` and
    ``raw_insert_text`` keep the new behaviour.

    ``read_error`` is None only if a caller builds the context by hand
    with ``focus_read_failed`` set, which production code does not do.
    The RuntimeError covers that case so the guard can never fall
    through and let a key be sent.
    """
    if not context.focus_read_failed:
        return
    if context.read_error is None:
        raise RuntimeError(
            f"{action_name}: the focused-control read failed and no error "
            "was recorded; refusing to send."
        )
    raise context.read_error


def _captured_hwnd_from_control(focused_control) -> Optional[int]:
    """Resolve a captured control to the window handle the proof compares.

    wh-review-pattern-fixes.45: the three selection paths record the
    control they copied from and must compare it later against
    ``GetForegroundWindow()``. Read the raw handle through the shared
    helper, then normalize it here so the value matches what the
    strategies and ``verified_paste`` produce for the same control.
    Returns None on any failure; every consumer treats None as "cannot
    prove" and sends nothing.
    """
    hwnd = top_level_hwnd_from_control(focused_control)
    if not hwnd:
        return None
    return normalize_hwnd_for_foreground_compare(hwnd)


class CapturedTarget(NamedTuple):
    """The control a selection was copied from, and its top-level window.

    wh-review-pattern-fixes.45: three paths copy a selection out of one
    control and then deliver a replacement somewhere else --
    ``transform_selection``, the selection branch of ``wrap_or_insert``,
    and the AI correction flow. Each one now carries this pair from the
    copy to the send, and the send only happens when the pair still
    holds the foreground.

    ``control`` is a live UIA control object. It cannot cross a process
    boundary, so the AI flow keeps it in the Input process and hands the
    Logic process a token instead (see
    ``UIActionHandler.capture_selected_text``).

    ``hwnd`` is a plain integer, already root-normalized by
    ``_captured_hwnd_from_control``. It does cross a process boundary, so the AI
    flow sends it to the Logic process and requires it back unchanged.
    It is None when the control could not be resolved to a window; every
    consumer treats that as "cannot prove" and refuses to send.
    """

    control: Any
    hwnd: Optional[int]


class UIActionHandler:
    """Orchestrates all UI interactions by delegating to specialist classes.

    This class is the main entry point for input_proc.py and maintains the
    same public API as the previous monolithic version. Internally, it delegates
    all work to focused specialist classes.

    NOTE: This class runs in the input_proc separate process. Unlike components
    in the main logic process that receive ConfigService via dependency injection,
    UIActionHandler receives a raw config dict because ConfigService cannot be
    pickled for inter-process communication.
    """

    def __init__(self, response_queue: Queue, config: dict, *, shutdown_event=None):
        """Initialize UI action handler with all specialist components.

        Args:
            response_queue: Queue for sending action responses
            config: Configuration dictionary (not ConfigService - can't pickle)
            shutdown_event: The Input process's shared shutdown signal, passed
                on to the continuous scroller so its timer thread can stop
                itself when the process is asked to close
                (wh-voice-access-parity.2.3.3.2.10). Keyword-only and
                optional: the command loop is not the only caller that builds
                this class, and a handler built without the signal keeps the
                behaviour it had before.
        """
        self.response_queue = response_queue
        self.config = config
        # Unified response abstraction (Schema A). All insertion paths must
        # route through this handler so each request yields exactly one
        # response on the demuxer queue (wh-lla5d).
        self.response = ResponseHandler(response_queue)

        # wh-n29v.41 / wh-n29v.42.1: Input-side defence-in-depth watermark for
        # pin_snapshot stale-generation rejection. A SINGLE bounded
        # (overlay_session_id, latest_accepted_paint_generation) pair -- NOT a
        # per-session dict, which would add one permanent entry per overlay
        # session for the lifetime of this long-lived process. overlay_session_id
        # is monotonic and the overlay state machine is single, so only the
        # latest session can receive a legitimate pin; an older session id (or a
        # strictly-older generation within the latest session) is stale.
        self._latest_pin_watermark: Optional[tuple[int, int]] = None

        # wh-overlay-slow-uia-stale-badges.8 part 4: one EXECUTED click per
        # (overlay_session_id, paint_generation). The pair of the last click
        # that returned ok; a later click carrying a pair that is not STRICTLY
        # newer is refused (stale_overlay_generation) -- that list was
        # consumed by the click that changed the screen. A single bounded
        # pair, not a per-session dict, for the same reason as
        # _latest_pin_watermark above (wh-n29v.42.1): overlay_session_id is
        # monotonic and the overlay machine is single. Ordering alone cannot
        # give Input a "current" generation (the command loop is FIFO, so an
        # arriving pair is always >= any watermark it could keep from paints),
        # which is why the guard keys on EXECUTION, the one event that
        # invalidates a painted list.
        self._last_executed_click_pair: Optional[tuple[int, int]] = None

        # wh-input-mouse-primitives: the SendInput seams for the mouse-grid
        # pointer actions (click_point / move_pointer / perform_drag). Set as
        # attributes rather than called directly so the test suite can inject
        # a fake and never synthesise real input; production keeps the
        # module-level Win32-backed wrappers above.
        self._mouse_click_seam = _win32_click_point
        self._mouse_move_seam = _win32_move_pointer
        self._mouse_drag_seam = _win32_drag_pointer
        # wh-voice-access-parity.2.3: the SendInput seam for the spoken scroll
        # commands. Same injection reason as the three pointer seams above,
        # though the wheel is not a grid action and is not gated by the
        # voice-clicking config (see ``scroll_wheel``).
        self._mouse_scroll_seam = _win32_scroll_wheel
        # wh-voice-access-parity.2.3.3: the one continuous scroll this process
        # may have running. It turns the wheel through ``_turn_the_wheel``
        # rather than through the seam directly, so it reads
        # ``_mouse_scroll_seam`` at call time -- a test that injects a fake
        # seam is then not silently bypassed by the continuous commands.
        # The shutdown signal goes with it: the timer thread outlives the
        # command that started it, so it is the one part of this class that
        # has to notice the process closing on its own
        # (wh-voice-access-parity.2.3.3.2.10).
        self._continuous_scroller = ContinuousScroller(
            self._turn_the_wheel, shutdown_event=shutdown_event,
        )

        # wh-review-pattern-fixes.45: the one selection target the AI
        # correction flow captured, held as (token, CapturedTarget).
        # ``capture_selected_text`` writes it and returns the token to
        # the Logic process; ``replace_selected_text`` requires the same
        # token back and then clears the slot. One slot is enough
        # because the Logic side holds ``AIService._processing_lock``
        # across the whole capture-model-replace sequence, so only one
        # transform can be in flight. A second capture overwrites the
        # slot, which invalidates the older token -- the correct result,
        # because the older capture no longer describes the screen.
        self._captured_selection_target: Optional[
            tuple[str, CapturedTarget]
        ] = None

        # wh-review-pattern-fixes.45: True when the last
        # ``_execute_insert_with_ack`` call refused to route because the
        # captured target had lost the foreground. ``transform_selection``
        # reads it to speak the specific reason instead of the generic
        # paste-failure message.
        self.last_insert_refused_for_focus_drift = False

        # Initialize all specialist components
        self.text_perfector = TextPerfector()
        self.clipboard = ClipboardOperations(config)
        # wh-ensure-focused-same-process-fallback: the focus manager needs
        # the same merged browser set the foreground checks use, so its
        # ensure_focused same-process fallback stays scoped to the
        # [ui_actions.foreground_check].same_process_browser_names list.
        # Resolved once here; VerifiedUnicodeStrategy below reuses it.
        same_process_names = resolve_same_process_browser_names(config)
        self.window_manager = WindowFocusManager(
            same_process_browser_names=same_process_names,
        )
        self.selection_transformer = SelectionTransformer()
        self.utterance_manager = UtteranceClipboardManager(
            timeout_seconds=config.get("ui_actions", {})
            .get("timing", {})
            .get("utterance_clipboard_timeout_seconds", 1.0)
        )

        self.buffer_manager = ShadowBufferManager()
        # wh-1g6er: the slim TerminalEditorProxy no longer mutates
        # clipboard state; clipboard_ops is retained on the signature
        # for parity with the legacy fixture.
        self.terminal_editor = TerminalEditorProxy(
            response_queue=self.response_queue,
            clipboard_ops=self.clipboard,
        )

        # wh-zndq / wh-fc1x: build the shared text-target predicate first
        # so the slow-path strategies can take a reference.
        self.text_target_predicate = build_predicate_from_config(config)
        # wh-7318z (wh-9weum Phase 2): rejection-text cache lives in the
        # input process so dictation text never crosses to logic. The
        # strategy generates a uuid4 token per rejection, stores
        # token -> text here, and emits the token in the IPC payload.
        # Phase 4 (wh-ftg63) reads back from this cache when the user
        # clicks Try-it-anyway.
        from .rejection_text_cache import RejectionTextCache

        self.rejection_text_cache = RejectionTextCache()
        # wh-b0sch: friendly-name resolver caches Win32 GetFileVersionInfo
        # results by process_id so the rejection toast can show the human
        # name (Zed) instead of the executable basename (zed.exe).
        from utils.file_version_info import get_default_resolver

        self._app_name_resolver = get_default_resolver()
        # wh-zib65: input-process first-rejection diagnostic log map.
        # Owned by the input process; the GUI side has its own
        # ToastSuppressionMap. The two maps do not coordinate.
        # wh-rejection-log-reescalation: an optional config window
        # ([ui_actions.text_target].rejection_reescalation_seconds)
        # lets the INFO line re-fire for a persistent rejection instead
        # of logging exactly once per key for the process lifetime.
        from rejection_rate_limit import (
            FirstRejectionLogMap,
            reescalation_seconds_from_config,
        )

        self._first_rejection_log_map = FirstRejectionLogMap(
            reescalation_seconds=reescalation_seconds_from_config(config)
        )
        self.rejected_strategy = RejectedInsertionStrategy(
            response_queue=self.response_queue,
            text_cache=self.rejection_text_cache,
            app_name_resolver=self._app_name_resolver,
            first_log_map=self._first_rejection_log_map,
            # wh-1r2b3.2.1: pass the text-target check's resolved
            # browser process set (DEFAULT plus any config additions
            # from [ui_actions.text_target].browser_process_names_extend)
            # so the categorizer matches what the check sees.
            browser_process_names=self.text_target_predicate._browser_processes,
            # wh-override-multiword-retry.1.1: pass the same
            # TextPerfector the rest of the input process uses so
            # multi-fragment aggregation composes punctuation cleanly
            # ("hello" then "." then "world" caches "hello. World"
            # rather than "hello . world").
            text_perfector=self.text_perfector,
        )

        # Initialize Strategies
        self.standard_strategy = StandardStrategy(
            self.buffer_manager,
            self.text_perfector,
            self.clipboard,
            self.window_manager,
            text_target_predicate=self.text_target_predicate,
        )
        self.flutter_strategy = FlutterStrategy(
            self.buffer_manager,
            self.text_perfector,
            self.clipboard,
            self.window_manager,
            text_target_predicate=self.text_target_predicate,
        )
        self.simple_paste_strategy = SimplePasteStrategy(
            self.clipboard,
            self.window_manager
        )
        # wh-9weum Phase 1 (wh-0ci9n): soft fallback for non-empty-
        # ClassName targets that the predicate could not positively
        # accept. Used by editors like Zed that render their own UI
        # and ship no UIA TextPattern. The text_perfector reference
        # lets DICTATION-mode pastes get the same leading-space and
        # sentence-start capitalization that StandardStrategy gets,
        # using a per-utterance mirror instead of the shadow buffer
        # (which cannot synchronize against a non-TextPattern target).
        self.clipboard_only_strategy = ClipboardOnlyStrategy(
            self.clipboard,
            self.window_manager,
            text_perfector=self.text_perfector,
        )
        # wh-606yk: Unicode SendInput strategy for short text in normal
        # apps. Reuses the same shadow buffer + text perfector + clipboard
        # provenance plumbing so the retraction counter and shadow buffer
        # stay consistent regardless of which strategy delivered the text.
        # wh-ix1z.22 / wh-fc1x.2: resolve the same-process browser list
        # from config once via the shared helper. The canonical list
        # lives in services/wheelhouse/config.toml under
        # [ui_actions.foreground_check].same_process_browser_names so
        # users can edit it without touching code; the resolver also
        # honors the wh-3nwy backward-compat extend key. Pass the
        # resolved frozenset to the strategy so its construction uses
        # the same merged set the verified_paste post-paste check uses
        # (ClipboardOperations resolves the same key in its own
        # __init__ from the same config dict). The set itself was
        # resolved once next to the WindowFocusManager construction
        # above (wh-ensure-focused-same-process-fallback).
        self.verified_unicode_strategy = VerifiedUnicodeStrategy(
            self.buffer_manager,
            self.text_perfector,
            self.clipboard,
            self.window_manager,
            same_process_browser_names=same_process_names,
        )
        # wh-r7al.1: composite that tries Unicode first and falls back to
        # StandardStrategy on a pre-send Unicode failure. Without this
        # wrapper, apps without UIA TextPattern (Chromium renderer
        # children, Electron, custom controls) regress from "clipboard
        # paste worked" to "Schema A error" the moment short text gets
        # routed to Unicode. The fallback gate is last_paste_was_sent: if
        # SendInput already fired, do NOT fall back, because Standard
        # would paste over partially landed Unicode characters.
        self.unicode_first_strategy = UnicodeFirstStrategy(
            self.verified_unicode_strategy,
            self.standard_strategy,
            self.clipboard,
        )

        # Initialize Router. The composite (UnicodeFirst) is what the
        # router sees as the Unicode strategy; the router's per-call
        # decision is purely a length check.
        verified_unicode_max_chars = (
            config.get("ui_actions", {})
            .get("verified_unicode", {})
            .get("max_chars", 50)
        )
        # wh-zndq / wh-fc1x: predicate and rejected strategy were built
        # above (before the strategy constructors so the slow-path
        # strategies could capture the predicate reference). Wire them
        # into the router so the router's own predicate check runs first
        # and the slow-path preflight catches stale-focus cases.
        self.router = InsertionRouter(
            self.standard_strategy,
            self.flutter_strategy,
            self.simple_paste_strategy,
            rejected_strategy=self.rejected_strategy,
            text_target_predicate=self.text_target_predicate,
            verified_unicode_strategy=self.unicode_first_strategy,
            verified_unicode_max_chars=verified_unicode_max_chars,
            clipboard_only_strategy=self.clipboard_only_strategy,
            # wh-elevated-target-notice: refuse administrator targets
            # before any delivery path runs; fail open on any doubt.
            elevation_checker=target_elevation_state,
        )

        # Letter buffer for auto-compression of spelled letters
        self._letter_buffer: list[str] = []

        # wh-paste-when-unverified.2: whether the most recent insertion
        # attempt was refused before the send (a rejection) rather than
        # failing some other way. end_utterance reads it to pick the log
        # level for a failed final letter-buffer flush -- see the
        # comment there for why the level matters.
        self._last_insert_was_rejected: bool = False

        # Retraction state (reset per utterance)
        self._user_interacted_during_utterance: bool = False
        self._used_simple_paste: bool = False
        # wh-lost-word-neighbour-paths.1.4: set when an insert retry
        # moved the remembered target while this utterance already
        # held credited characters. See the set site in
        # _execute_insert_with_ack's retry loop.
        self._retry_rebound_target: bool = False
        # wh-lost-word-neighbour-paths.1.5: the windows this
        # utterance credited characters to. Empty means nothing was
        # recorded, one element means every credit went to the same
        # window, and two or more means the credits are split. The
        # retry guard reads it through _retry_returned_to_credited.
        # A zero member stands for a delivery whose window could not
        # be read, so it can never equal a real window and always
        # leaves the set looking split.
        self._credited_target_hwnds: set[int] = set()

        logger.debug("UIActionHandler initialized with all specialist components.")

    # ========================================================================
    # PUBLIC API - Open editor for redirect (wh-pkhrp.1.1)
    # ========================================================================

    def open_editor_for_redirect(
        self,
        request_id: Optional[str] = None,
        terminal_hwnd: Optional[int] = None,
        **kwargs,
    ) -> None:
        """Open the terminal dictation editor with EMPTY initial text.

        wh-pkhrp.1.1 (Approach A): the focus-redirect path buffers the
        triggering word in Logic and drains it (along with any
        subsequent words) on FOCUS_CONFIRMED. The editor must be
        opened without dictating any text -- otherwise the triggering
        word would land twice (once via the open path, once via the
        drain).

        When the IPC carries ``terminal_hwnd`` (wh-pkhrp.3.3), the
        handler positions the editor against THAT window's bounding
        rect rather than re-resolving the current focused control via
        UIA. Logic's focus-redirect policy already resolved the target
        terminal HWND before sending this IPC, so honouring the
        provided value closes the foreground-drift race between
        Logic's decision and Input's UIA capture. Falls back to the
        UIA capture only when ``terminal_hwnd`` is missing.

        Always emits a Schema A response so the caller's Future
        resolves; the action is NOT listed in
        ``_HANDLES_OWN_RESPONSE`` because Logic's open IPC is
        fire-and-forget (no request_id under normal use) but the
        path threads request_id through for parity with other
        terminal IPCs.
        """
        try:
            hwnd, rect = self._resolve_redirect_target(terminal_hwnd)
            if not hwnd:
                logger.warning(
                    "open_editor_for_redirect: could not resolve target HWND",
                )
                self.response.send_error(
                    request_id,
                    "open_editor_for_redirect",
                    "no target HWND",
                )
                return
            if not rect:
                logger.warning(
                    "open_editor_for_redirect: no bounding rect for hwnd=%s",
                    hwnd,
                )
                self.response.send_error(
                    request_id,
                    "open_editor_for_redirect",
                    "no bounding rect",
                )
                return
            # wh-1g6er: open the editor directly through the proxy
            # with empty text. The drain words flow through
            # StandardStrategy / VerifiedUnicodeStrategy against the
            # editor's QPlainTextEdit (which has UIA TextPattern, so
            # the predicate accepts) on FOCUS_CONFIRMED -- the proxy
            # is only responsible for opening / closing the editor,
            # not for the inserted text.
            rid = self.terminal_editor.show(
                initial_text="",
                terminal_hwnd=hwnd,
                geometry=rect,
            )
            shown = rid is not None
            if shown:
                self.response.send_success(
                    request_id,
                    "open_editor_for_redirect",
                    ResponseHandler.PATH_INSERT_VERIFIED,
                )
            else:
                self.response.send_error(
                    request_id,
                    "open_editor_for_redirect",
                    "show send failed",
                )
        except Exception as e:
            logger.error(
                "open_editor_for_redirect failed: %s", e, exc_info=True,
            )
            self.response.send_error(
                request_id,
                "open_editor_for_redirect",
                str(e),
            )

    def _resolve_redirect_target(
        self,
        terminal_hwnd: Optional[int],
    ) -> tuple[Optional[int], Optional[tuple]]:
        """Resolve (hwnd, bounding rect) for ``open_editor_for_redirect``.

        wh-pkhrp.3.3: prefer the IPC-supplied ``terminal_hwnd``. The
        Logic-side policy has already resolved this; a UIA re-capture
        here would race a foreground drift between policy decision
        and IPC arrival. Falls back to ``capture_context()`` /
        ``GetTopLevelControl`` only when ``terminal_hwnd`` is missing
        (legacy callers).
        """
        if terminal_hwnd:
            rect = self._bounding_rect_from_hwnd(terminal_hwnd)
            return terminal_hwnd, rect
        context = capture_context()
        if not context.focused_control:
            return None, None
        top_level = context.focused_control.GetTopLevelControl()
        if top_level is None:
            return None, None
        hwnd = top_level.NativeWindowHandle if hasattr(top_level, "NativeWindowHandle") else 0
        rect = None
        if hasattr(top_level, "BoundingRectangle"):
            br = top_level.BoundingRectangle
            if br.width() > 0 and br.height() > 0:
                rect = (br.left, br.top, br.right, br.bottom)
        return hwnd or None, rect

    def _bounding_rect_from_hwnd(self, hwnd: int) -> Optional[tuple]:
        """Return (left, top, right, bottom) for ``hwnd``, or None.

        wh-pkhrp.3.3: ``GetWindowRect`` returns physical pixels on
        Windows, matching the format the QDialog geometry code
        already expects (UIA BoundingRectangle is also physical
        pixels). A failure returns None so callers fall back to a
        sensible default geometry.
        """
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception as exc:
            logger.debug(
                "_bounding_rect_from_hwnd: GetWindowRect(%s) failed: %s",
                hwnd, exc,
            )
            return None
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)

    # ========================================================================
    # PUBLIC API - Buffer Management
    # ========================================================================

    def invalidate_buffer(self, source: str = "unknown"):
        """Invalidate the shadow buffer cache.

        Called by input_proc.py when user input detected (mouse/keyboard).
        """
        self.buffer_manager.invalidate()
        self._user_interacted_during_utterance = True

    # ========================================================================
    # PUBLIC API - Utterance Lifecycle
    # ========================================================================

    def start_utterance(self, utterance_id: int, **kwargs):
        """Save clipboard once at utterance start.

        Args:
            utterance_id: The ID of the utterance starting
        """
        # Reset retraction tracking for new utterance
        self._user_interacted_during_utterance = False
        self._used_simple_paste = False
        self._retry_rebound_target = False
        self._credited_target_hwnds.clear()
        self.clipboard.reset_paste_counter()
        # wh-9weum Phase 1: clear the soft-fallback strategy's
        # preceding-chars mirror so the first word of the new
        # utterance perfects with sentence-start capitalization.
        # Mirrors terminal_strategy.reset_editor_mirror's purpose
        # for the terminal editor session boundary.
        self.clipboard_only_strategy.reset_preceding_mirror()

        self.utterance_manager.start_utterance(utterance_id)

    def end_utterance(self, utterance_id: Optional[int] = None, **kwargs):
        """Restore clipboard once at utterance end.

        Args:
            utterance_id: The ID of the utterance ending (optional)
        """
        # Flush any buffered letters with compression before ending utterance.
        # wh-review-pattern-fixes.23: end_utterance never receives a
        # request_id (it is not in _HANDLES_OWN_RESPONSE; the generic
        # dispatcher owns its response), so the log is its existing
        # failure channel. A failed final flush must not vanish, and it
        # must not abort the clipboard-restore cleanup below either --
        # continue after logging.
        #
        # wh-paste-when-unverified.2: the level depends on WHY nothing
        # was delivered. An ERROR record IS a Windows notification --
        # ErrorNotificationHandler (utils/error_notifier.py) is a
        # logging handler at ERROR level that utils/logging_setup.py
        # attaches to the root logger -- so a deliberate pre-send
        # refusal must log at WARNING. Every other failure keeps ERROR.
        if not self._flush_letter_buffer():
            if self._last_insert_was_rejected:
                logger.warning(
                    "end_utterance: final letter-buffer flush was "
                    "refused before the send; the buffered letters "
                    "were not delivered."
                )
            else:
                logger.error(
                    "end_utterance: final letter-buffer flush failed; the "
                    "buffered letters were not delivered."
                )

        # Skip clipboard restore when a submit is still pasting.
        # Clipboard operations (win32clipboard) on the main thread race with
        # the GUI-side submit pipeline (paste + enter), and a restore racing
        # against an in-flight paste produced STATUS_HEAP_CORRUPTION
        # 0xC0000374 in the legacy terminal strategy. The submit_in_progress
        # check still covers any utterance ending while the editor's paste
        # has not yet completed.
        if self.terminal_editor._submit_in_progress.is_set():
            self.utterance_manager.skip_clipboard_restore()

        self.utterance_manager.end_utterance(utterance_id)

        # Reset retraction tracking (safety cleanup)
        self._user_interacted_during_utterance = False
        self._used_simple_paste = False
        self._retry_rebound_target = False
        self._credited_target_hwnds.clear()
        self.clipboard.reset_paste_counter()

    def skip_clipboard_restore(self, enable: bool = True, **kwargs):
        """Set the skip restoration flag for copy/cut commands.

        This prevents clipboard restoration at utterance end, allowing
        commands that intentionally modify the clipboard (copy, cut, etc.)
        to work correctly.

        Args:
            enable: If True, skip clipboard restoration (default: True)
        """
        if enable:
            self.utterance_manager.skip_clipboard_restore()

    def clear_skip_clipboard_restore(self, **kwargs):
        """Clear the skip flag after command execution.

        This is called automatically by command_engine after every command
        completes to prevent state leakage between commands.
        """
        self.utterance_manager.clear_skip_flag()

    # ========================================================================
    # PUBLIC API - Retraction
    # ========================================================================

    def retract(self) -> dict:
        """Retract pasted text by sending backspaces.

        Checks safety gates before retracting (wh-t81d9.1):
        0. Nothing pasted and no buffered letters -> block
           (nothing_to_retract) BEFORE every other gate
           (wh-click-number-dictation). The gates below protect the
           backspace side effect; with zero pasted characters there is no
           side effect, and they can misfire on stale state from a
           PREVIOUS utterance -- the live case was a command-buffered
           utterance (nothing typed) whose remembered paste target came
           from an earlier utterance while the foreground had legitimately
           changed, so retract answered focus_drifted and SpeechProcessor
           dropped the corrected final instead of replaying it.
        1. User interaction during utterance -> block (user_interacted)
        2. SimplePaste strategy was used -> block (simple_paste)
        3. Optimistic paste under clipboard contention -> block
           (paste_unverified).
        4. Remembered target HWND no longer foreground -> block
           (focus_drifted).
        5. Nothing pasted and no buffered letters -> block
           (nothing_to_retract). Kept as the in-order fallback for the
           counter the Qt branch below selects; gate 0 already caught the
           both-counters-zero case.
        6. Grapheme-unsafe paste against a Qt-backed target -> block
           (qt_grapheme_unsafe).
        7. Buffered letters but no paste -> drop letter buffer, report
           ``retracted`` so SpeechProcessor replays the corrected final
           (wh-j3mgc).
        8. Current focus is not a text target (per the shared predicate)
           -> block (text_target_rejected). Skipped on Flutter and when
           no predicate is wired.
        9. All clear -> send backspaces. If SendInput reports partial
           delivery, return ``partial_send`` so the consumer does not
           replay on top of a half-deleted region. Otherwise drop any
           letter buffer and return ``retracted``.

        Returns:
            Dict with 'status' ('retracted' or 'not_retracted'),
            'reason' (explanation), and optionally 'chars' (count
            retracted) or 'chars_sent' (partial-delivery count).
        """
        if (
            self.clipboard.accumulated_paste_chars == 0
            and self.clipboard.accumulated_paste_clusters == 0
            and not self._letter_buffer
        ):
            logger.info("Retraction blocked: no characters pasted")
            return {"status": "not_retracted", "reason": "nothing_to_retract"}

        if self._user_interacted_during_utterance:
            logger.info("Retraction blocked: user interacted during utterance")
            return {"status": "not_retracted", "reason": "user_interacted"}

        if self._used_simple_paste:
            logger.info("Retraction blocked: SimplePaste strategy was used")
            return {"status": "not_retracted", "reason": "simple_paste"}

        # wh-lost-word-neighbour-paths.1.4: a retry moved the remembered
        # target after this utterance had already credited characters, so
        # the accumulated total no longer belongs to a single window.
        # Backspaces follow the foreground window, so sending them would
        # chew into text the user typed there. The set site in
        # _execute_insert_with_ack's retry loop explains the accounting.
        #
        # wh-lost-word-neighbour-paths.1.5 narrowed which retries set
        # it: a retry that came back to the one window every credited
        # character already went to leaves the flag clear, because the
        # backspaces would reach exactly those characters.
        if self._retry_rebound_target:
            logger.info(
                "Retraction blocked: a retry moved the target window "
                "while this utterance already held credited characters",
            )
            return {
                "status": "not_retracted", "reason": "retry_rebound_target",
            }

        # Fail-closed gates against provenance the rest of the pipeline
        # already records (wh-20yil, wh-t81d9.1). Sending backspaces under
        # any of these signals can chew into pre-existing user text.
        if self.clipboard.last_paste_was_optimistic:
            logger.info("Retraction blocked: last paste was optimistic / unverified")
            return {"status": "not_retracted", "reason": "paste_unverified"}

        # Focus verification. Backspaces follow the foreground window, so a
        # focus shift between paste and retract would chew into the wrong
        # window's content.
        #
        # wh-oe7u.3: both expected and observed HWNDs are root-normalized
        # so Chromium/Electron renderer children compare equal to their
        # top-level frame. Fail closed on any normalization failure on
        # either side to match the verified_paste contract.
        remembered_hwnd = self.window_manager._last_target_hwnd
        if remembered_hwnd:
            expected_root = normalize_hwnd_for_foreground_compare(remembered_hwnd)
            if expected_root is None:
                logger.info(
                    "Retraction blocked: remembered HWND %s could not be normalized",
                    remembered_hwnd,
                )
                return {"status": "not_retracted", "reason": "focus_drifted"}
            try:
                foreground = win32gui.GetForegroundWindow()
            except Exception as e:
                logger.warning(f"GetForegroundWindow failed during retract: {e}")
                return {"status": "not_retracted", "reason": "focus_drifted"}
            actual_root = normalize_hwnd_for_foreground_compare(foreground)
            if actual_root is None:
                logger.info(
                    "Retraction blocked: foreground HWND %s could not be normalized",
                    foreground,
                )
                return {"status": "not_retracted", "reason": "focus_drifted"}
            if actual_root != expected_root:
                logger.info(
                    "Retraction blocked: remembered root %s != foreground root %s "
                    "(remembered hwnd=%s, foreground hwnd=%s)",
                    expected_root, actual_root, remembered_hwnd, foreground,
                )
                return {"status": "not_retracted", "reason": "focus_drifted"}

        # wh-pkhrp.2: branch the backspace count on whether any paste
        # in this utterance landed in a Qt-backed target. The
        # ``credit_paste_chars`` helper sets
        # ``accumulated_paste_was_qt`` when a paste's target class
        # name matches the Qt prefix convention; the focus-drift
        # gate above guarantees the foreground window has not changed
        # between paste and retract, so the sticky flag remains
        # accurate at this point. Qt apps delete by grapheme cluster,
        # so retract sends one backspace per cluster; everywhere else
        # uses the historical Python-code-point count.
        if self.clipboard.accumulated_paste_was_qt:
            char_count = self.clipboard.accumulated_paste_clusters
        else:
            char_count = self.clipboard.accumulated_paste_chars
        had_buffered_letters = bool(self._letter_buffer)

        if char_count == 0 and not had_buffered_letters:
            logger.info("Retraction blocked: no characters pasted")
            return {"status": "not_retracted", "reason": "nothing_to_retract"}

        # wh-pkhrp.2 (prev. wh-pkhrp.1.7): the grapheme-unsafe
        # fail-closed gate that returned reason='qt_grapheme_unsafe'
        # has been removed. Qt-backed targets now use the parallel
        # ``accumulated_paste_clusters`` counter selected above; the
        # ``accumulated_has_grapheme_unsafe`` flag stays as an
        # informational signal for structured logging only.
        if (
            char_count > 0
            and self.clipboard.accumulated_has_grapheme_unsafe
        ):
            logger.debug(
                "Retract carrying grapheme-unsafe insertion: was_qt=%s "
                "code_units=%d clusters=%d -> using char_count=%d",
                self.clipboard.accumulated_paste_was_qt,
                self.clipboard.accumulated_paste_chars,
                self.clipboard.accumulated_paste_clusters,
                char_count,
            )

        if char_count == 0 and had_buffered_letters:
            # Letters are still queued in _letter_buffer; nothing has hit
            # the screen yet. Drop the buffer so end_utterance does not
            # flush stale letters, and tell the caller to replay the
            # corrected final (wh-j3mgc).
            buffered = list(self._letter_buffer)
            logger.info(
                "Retracting buffered letters (no paste yet): "
                f"{redact_transcript(' '.join(buffered))}"
            )
            self._letter_buffer.clear()
            self.buffer_manager.invalidate()
            return {
                "status": "retracted",
                "chars": 0,
                "reason": "letter_buffer_cleared",
            }

        # wh-32d / wh-fc1x: text-target predicate gate. Runs AFTER the
        # buffered-letters early-return paths above (wh-ix1z.18 fix --
        # the gate must not block letter-buffer cleanup which never
        # sends Backspace) and immediately BEFORE send_backspaces. The
        # HWND focus check earlier proves the foreground window has not
        # changed, but two controls inside the SAME top-level window
        # can have very different roles -- one a real text input, the
        # other a list item, button, document body, or other non-text
        # element. Sending Backspace to a non-text control can trigger
        # page navigation (browsers), close menus, or deselect items
        # rather than delete from the original text input.
        #
        # Run the shared predicate against the CURRENT focused control
        # (not the captured target). Skipped on Flutter (Flutter widgets
        # often do not expose UIA TextPattern but are legitimate
        # retraction targets for FlutterStrategy). Skipped when no
        # predicate is wired (legacy back-compat).
        if self.text_target_predicate is not None:
            current_context = capture_context()
            if not current_context.is_flutter:
                verdict = self.text_target_predicate.evaluate(
                    current_context.focused_control,
                    class_name="",
                    process_name=current_context.process_name or "",
                )
                if not verdict.verdict:
                    logger.info(
                        "Retraction blocked: current focus is not a "
                        "text target -- reason=%s control_type=%s "
                        "class=%s process=%s",
                        verdict.reason, verdict.control_type or "?",
                        verdict.class_name or "?",
                        verdict.process_name or "?",
                    )
                    return {
                        "status": "not_retracted",
                        "reason": "text_target_rejected",
                    }

        # All gates passed - send backspaces. SendInput can report partial
        # delivery; if it does, we have no way to know how much of the
        # editor's content was actually deleted, so refuse to claim
        # success (wh-t81d9.1).
        logger.info(f"Retracting {char_count} characters via backspaces")
        # wh-review-pattern-fixes.8: invalidate before the backspace
        # dispatch (was after it), so the partial_send outcome below also
        # leaves the buffer invalid -- an unknown number of backspaces
        # landed (see the hotkey_action block comment for the rationale).
        self.buffer_manager.invalidate()
        delivered = send_backspaces(char_count)
        if not delivered:
            logger.error(
                "Retraction partial: SendInput rejected events for %d backspaces",
                char_count,
            )
            return {
                "status": "not_retracted",
                "reason": "partial_send",
                "chars_sent": char_count,
            }

        self.clipboard.reset_paste_counter()
        # Drop any pending letter buffer so end_utterance does not flush
        # stale letters on top of the replayed final (wh-j3mgc).
        if had_buffered_letters:
            self._letter_buffer.clear()

        return {"status": "retracted", "chars": char_count, "reason": f"retracted_{char_count}_chars"}

    # ========================================================================
    # LETTER BUFFER FOR AUTO-COMPRESSION
    # ========================================================================

    def _is_single_letter(self, text: str) -> bool:
        """Check if text is a single alphabetic letter."""
        return len(text) == 1 and text.isalpha()

    def _flush_letter_buffer(self) -> bool:
        """Flush buffered letters with auto-compression applied.

        If buffer has 3+ letters, compresses them to a single word.
        Otherwise, outputs letters as-is separated by spaces.

        Returns the delivery outcome (wh-review-pattern-fixes.23): True
        when nothing was buffered or the direct insert delivered the
        compressed text; False when delivery failed. On failure the
        letters are NOT re-buffered and the flush is NOT replayed -- a
        False strategy result can mean partial delivery, so a blind
        replay could double-insert. Instead the shadow buffer is
        invalidated (the conservative-state model from the
        ClipboardOnly branch in _execute_insert_with_ack) so the next
        compose re-syncs via UIA, and the caller decides how to
        surface the failure.
        """
        if not self._letter_buffer:
            return True

        # Join letters with spaces and apply compression
        buffered_text = " ".join(self._letter_buffer)
        compressed_text = auto_compress_spelled_letters(buffered_text)

        logger.info(f"[LETTER_BUFFER] Flushing: '{redact_transcript(buffered_text)}' -> '{redact_transcript(compressed_text)}'")

        # Clear buffer before inserting to prevent recursion
        self._letter_buffer.clear()

        # Insert the compressed text directly (bypass buffering logic)
        delivered = self._do_direct_insert(compressed_text, request_id=None)
        if not delivered:
            logger.warning(
                "[LETTER_BUFFER] Flush delivery failed; buffered letters "
                "were not (or only partially) delivered."
            )
            self.buffer_manager.invalidate()
        return delivered

    def _do_direct_insert(self, text: str, request_id: Optional[str] = None) -> bool:
        """Insert text directly without letter buffering.

        Returns the delivery outcome from _execute_insert_with_ack
        (wh-review-pattern-fixes.23): True only when the strategy
        delivered the text.
        """
        if self.utterance_manager.is_in_utterance():
            return self._execute_insert_with_ack(text, request_id)
        else:
            with clipboard_context(restore_delay=0.05):
                return self._execute_insert_with_ack(text, request_id)

    # ========================================================================
    # PUBLIC API - Main Text Insertion
    # ========================================================================

    def intelligent_insert_text(
        self,
        insertion_string: str,
        request_id: Optional[str] = None,
        target_hwnd: Optional[int] = None,
        *,
        defer_single_letter: bool = True,
        **kwargs,
    ) -> bool:
        """Acts as a dispatcher for dictation, routing to appropriate handler.

        Decision Flow (order matters!):
        0. Check for single letter → buffer for auto-compression
        1. Terminal editor already active? → Append to existing session
        2. Capture Context (Control, Flutter, Terminal)
        3. Router selects Strategy
        4. Strategy executes insertion

        Args:
            insertion_string: Text to insert
            request_id: Optional request ID for response tracking
            target_hwnd: Optional foreground HWND the caller expects to
                be present at dispatch time. When supplied (wh-pkhrp.3.10
                Phase 3 redirect drain), the handler re-verifies the
                Windows foreground matches before any keystroke /
                clipboard work. A mismatch refuses the insert with a
                structured failure -- without this re-check the editor's
                ``focus_confirmed`` ack could race a user click back to
                the terminal and the drained words would land in the
                shell prompt.
            defer_single_letter: When True (default), a single
                alphabetic letter is appended to the letter buffer for
                deferred delivery and this call returns True without
                touching the screen -- the deliberate design for
                ordinary dictation (wh-review-pattern-fixes.11). When
                False, a single letter skips the letter-buffer branch
                and takes the normal flush-then-strategy path: any
                pending buffered letters flush first (preserving
                dictation order), then the letter goes through a real
                insertion strategy, which owns the Schema A response.
                wrap_or_insert's Priority 2 empty-delimiter call passes
                False (wh-review-pattern-fixes.16): buffering a
                single-letter delimiter would insert nothing physically,
                return True, and let the caller's left-arrow press shift
                the caret before the deferred flush lands the letter.

        Returns:
            The delivery outcome (wh-review-pattern-fixes.11). True means
            the chosen strategy delivered the text. False means nothing
            landed: a strategy failure, a handled insertion exception, a
            foreground-mismatch refusal, or a pre-send rejection
            (RejectedInsertionStrategy resolves the Future as a success,
            but it delivers no text). Callers that chain a dependent
            action on the inserted text (wrap_or_insert's caret move)
            must gate on this value. The buffered single-letter path
            returns True: the letter is accepted for deferred delivery
            and the emitted response already reports success.
            wh-review-pattern-fixes.23: a failed letter-buffer flush
            also returns False -- the buffered letters did not land, so
            the current insertion is skipped (order preservation) and
            the request_id gets a Schema A error.
        """
        # LETTER BUFFERING: Buffer single letters for auto-compression
        # When we see a non-single-letter word, flush buffer first.
        # wh-review-pattern-fixes.16: defer_single_letter=False skips the
        # buffering branch so the letter falls through to the else arm
        # below -- which flushes any pending buffered letters first and
        # then routes the letter through a real insertion strategy.
        if defer_single_letter and self._is_single_letter(insertion_string):
            self._letter_buffer.append(insertion_string)
            logger.debug(f"[LETTER_BUFFER] Buffered letter: '{redact_transcript(insertion_string)}', buffer now: {len(self._letter_buffer)} letters")
            # Single letters defer the actual paste until the buffer flushes,
            # so emit the response right away and let the caller continue
            # (wh-lla5d). Schema A format, consumed by app.py demuxer.
            self.response.send_success(
                request_id,
                "intelligent_insert_text",
                ResponseHandler.PATH_HEURISTIC_DONE,
            )
            return True
        else:
            # Non-single-letter word: flush any buffered letters first
            if self._letter_buffer:
                if not self._flush_letter_buffer():
                    # wh-review-pattern-fixes.23: the buffered letters
                    # did not land. Skip the current insertion instead
                    # of writing it after the lost letters -- delivering
                    # the word now would put out-of-order text on
                    # screen, which no later replay can repair. A clean
                    # failure leaves the target unchanged (beyond any
                    # partial flush delivery, which the flush already
                    # marked by invalidating the shadow buffer) so the
                    # caller can replay the corrected final. Emit the
                    # single Schema A error this request_id is owed
                    # (no-op when request_id is None, e.g.
                    # wrap_or_insert Priority 2, which owns its own
                    # response and gates on the False return).
                    logger.warning(
                        "intelligent_insert_text: letter-buffer flush "
                        "failed; skipping insertion of the current text."
                    )
                    self.response.send_error(
                        request_id,
                        "intelligent_insert_text",
                        "letter buffer flush failed; insertion skipped",
                    )
                    return False

        # wh-pkhrp.3.10: TOCTOU narrowing. If the caller pinned the
        # expected target HWND (Phase 3 redirect drain), verify the
        # foreground matches immediately before context capture and
        # strategy dispatch. Any mismatch refuses the insert with a
        # structured failure so the drained words never land in the
        # wrong window. The check is intentionally placed AFTER the
        # letter-buffer fast-return: single letters are deferred and
        # the drain path never sends single letters with target_hwnd,
        # so the gate stays before any real SendInput / paste work.
        if target_hwnd:
            if not self._foreground_matches_target(target_hwnd):
                logger.warning(
                    "intelligent_insert_text: foreground does not match "
                    "caller-provided target_hwnd=%s; refusing insert.",
                    target_hwnd,
                )
                self.response.send_error(
                    request_id,
                    "intelligent_insert_text",
                    "foreground_mismatch",
                )
                return False

        # wh-4z4g9: dirty tracking moved to _execute_insert_with_ack so it
        # only fires when the chosen strategy actually wrote the system
        # clipboard. _last_paste_time is also moved there so the
        # wrap_or_insert "recent paste" heuristic does not falsely fire on
        # a Unicode-only utterance that never touched the clipboard.

        if self.utterance_manager.is_in_utterance():
            return self._execute_insert_with_ack(insertion_string, request_id)
        else:
            with clipboard_context(restore_delay=0.05):
                return self._execute_insert_with_ack(
                    insertion_string, request_id
                )

    def _foreground_matches_target(self, target_hwnd: int) -> bool:
        """Return True when the foreground HWND matches ``target_hwnd``.

        wh-pkhrp.3.10: shrinks the TOCTOU window between a Logic-side
        focus_confirmed decision and the Input-side dispatch to a
        single ``GetForegroundWindow`` call. Normalises both sides via
        ``GA_ROOT`` so a Chromium-style renderer child compares equal
        to its top-level frame; on any normalisation failure the
        check returns False (fail closed).
        """
        try:
            foreground = win32gui.GetForegroundWindow()
        except Exception as exc:
            logger.debug(
                "intelligent_insert_text: GetForegroundWindow failed: %s",
                exc,
            )
            return False
        expected_root = normalize_hwnd_for_foreground_compare(target_hwnd)
        actual_root = normalize_hwnd_for_foreground_compare(foreground)
        if expected_root is None or actual_root is None:
            return False
        return expected_root == actual_root

    def _record_credited_target(self, context, result) -> None:
        """Record the window a successful delivery targeted.

        wh-lost-word-neighbour-paths.1.5: the retraction counters in
        ClipboardOperations hold one per-utterance total and record
        nothing about which window each credit went to. This records it
        alongside them, so the retry guard can tell a retry that moved
        to a new window from a retry that came back to the window the
        earlier words went to.

        Called at every delivery call site in this module after the
        strategy reports success. A site that is missed leaves the set
        empty, and the guard then refuses exactly as it did before this
        change. A delivery whose window cannot be read adds 0, which no
        real window equals, so the set reads as split and the guard
        refuses for that too.

        crewcut: this records the window the capture TARGETED, not the
        window the keystroke was proven to reach, and the two can
        disagree on a VERIFIED delivery. The known case is a Brave text
        control under an invisible helper top-level window while the
        main window holds the foreground. The capture carries the helper
        as its root, the post-paste check in
        ClipboardOperations.verified_paste accepts the unequal pair
        through same_process_fallback_matches, the characters reach the
        main window, and this records the helper. A later retry that
        comes back to the main window then misses the recorded helper,
        so the guard refuses the retraction even when every credit went
        to that main window: the earlier words stay on screen and the
        corrected final is not typed. The refusal is the safe direction,
        because a refused retraction sends no backspaces; it is accepted
        as a known limit. To remove the limit, record both windows of
        each delivery and match on them, or have
        ClipboardOperations.credit_paste_chars record the proven
        recipient itself and read it from there. Finding
        wh-lost-word-neighbour-paths.1.6 holds the code reading and what
        it leaves unproven.
        """
        if not getattr(result, "success", False):
            return
        self._credited_target_hwnds.add(_context_hwnd(context) or 0)

    def _retry_returned_to_credited(self, context) -> bool:
        """True when this retry captured the one window already credited.

        wh-lost-word-neighbour-paths.1.5, boss ruling case A term 2:
        refuse by default. False when nothing was recorded, when the
        credits are split across windows, when this capture has no
        readable window, and when the captured window is not the
        credited one. Only a single credited window that matches this
        capture answers True.
        """
        credited = self._credited_target_hwnds
        if len(credited) != 1:
            return False
        hwnd = _context_hwnd(context)
        return bool(hwnd) and hwnd in credited

    def _execute_insert_with_ack(
        self,
        insertion_string,
        request_id,
        options: Optional[InsertionOptions] = None,
        action_name: str = "intelligent_insert_text",
    ):
        """Execute insertion and emit exactly one truthful Schema A response.

        Honors ``InsertionResult.success`` from ``strategy.insert``: True
        emits a Schema A success with path='insert_verified'; False emits
        a Schema A error (wh-d43oi). A False return means the strategy
        detected a real failure (e.g. clipboard verification failed,
        shadow buffer sync failed). The caller's Future must see that
        failure instead of a silent success.

        Also routes ``InsertionResult.clipboard_dirty`` to the utterance
        manager (wh-4z4g9). Only strategies that actually wrote the
        system clipboard set this; the manager uses the flag to gate
        end-of-utterance restoration so a Unicode-only utterance does
        not clobber the user's clipboard.

        wh-iti5: ``options`` threads InsertionMode through to the chosen
        strategy. ``action_name`` lets callers like ``verbatim_insert_text``
        emit a Schema A response that the demuxer / pipeline tracing can
        attribute correctly.

        Returns the delivery outcome (wh-review-pattern-fixes.11): True
        only when the strategy delivered the text. A pre-send rejection
        emits a Schema A success (PATH_INSERT_REJECTED, wh-zndq) but
        delivers nothing, so it returns False, as do strategy failures
        and handled exceptions.

        wh-review-pattern-fixes.45: when ``options`` carries a captured
        target, this method proves that target still holds the
        foreground BEFORE it captures a fresh context. Without the
        proof, a selection copied from control A and transformed here
        would route against whatever control the fresh capture returned,
        and the transformation would replace a selection in a different
        window. Ordinary dictation supplies no captured target and is
        unaffected.
        """
        self.last_insert_refused_for_focus_drift = False
        # wh-paste-when-unverified.2: cleared per attempt so a refusal
        # recorded below cannot leak into the next insertion, nor into
        # an attempt that ends in the focus-drift refusal or the
        # exception handler (neither of which reads a result).
        self._last_insert_was_rejected = False
        try:
            # wh-review-pattern-fixes.45: prove the captured target
            # first. The proof activates the captured window, focuses
            # the captured control, and then compares the foreground
            # against the captured HWND. It is the same proof
            # verified_paste and VerifiedUnicodeStrategy run before
            # their own sends (wh-review-pattern-fixes.41), so there is
            # one comparison in the code, not three.
            captured_control = (
                options.captured_target_control if options else None
            )
            captured_hwnd = (
                options.captured_target_hwnd if options else None
            )
            if captured_control is not None or captured_hwnd is not None:
                if not self.clipboard.prove_captured_target_is_foreground(
                    self.window_manager,
                    captured_hwnd,
                    captured_control,
                    caller=action_name,
                ):
                    self.last_insert_refused_for_focus_drift = True
                    logger.error(
                        "%s: the captured selection target "
                        "(hwnd=%s) no longer holds the foreground; "
                        "refusing to insert.",
                        action_name, captured_hwnd,
                    )
                    self.response.send_error(
                        request_id,
                        action_name,
                        "captured target lost the foreground; "
                        "nothing was inserted",
                    )
                    return False

            # wh-paste-target-window-vanished: two attempts at most.
            # The second one runs only when the first refused before
            # any keystroke because the window it captured no longer
            # exists. That failure delivered the word nowhere, so one
            # more attempt cannot deliver it twice. Every other outcome
            # leaves this loop on the first pass, exactly as before.
            attempt = 0
            captured_process_name = ""
            while True:
                # Capture Context
                context = capture_context()
                # wh-paste-target-window-vanished, boss ruling option
                # 2: the word must not land in a different program from
                # the one the user spoke it into. The capture recorded
                # the program name while the window was still alive, so
                # this comparison needs no Windows call and no live
                # handle. An empty name means the capture could not read
                # the program, and two unreadable names must not compare
                # equal, so the retry is refused there too.
                if attempt == 1 and (
                    not context.process_name
                    or context.process_name != captured_process_name
                ):
                    logger.info(
                        "%s: the captured window is gone and the newly "
                        "captured target belongs to %s, not %s; the "
                        "word is not delivered.",
                        action_name,
                        context.process_name or "an unreadable program",
                        captured_process_name or "an unreadable program",
                    )
                    break
                captured_process_name = context.process_name

                # Remember window handle for focus restoration
                if context.focused_control:
                    self.window_manager.remember_target(context.focused_control)
                    # wh-lost-word-neighbour-paths.1.4, boss ruling
                    # 9: the retry has just moved the remembered
                    # target to a different window. The retraction
                    # counter is one per-utterance total that
                    # records nothing about which window each
                    # credit went to, so characters credited
                    # earlier in this utterance do not belong to
                    # the window the retry chose, and sending them
                    # there would delete text the user typed.
                    # retract() refuses while this flag stands.
                    # The condition names no strategy class, so it
                    # covers the ClipboardOnly route as well as the
                    # default paths, and it reads the counters
                    # rather than the mere fact of a retry, so an
                    # utterance that has credited nothing yet still
                    # retracts normally.
                    #
                    # wh-lost-word-neighbour-paths.1.5, boss ruling
                    # case A: a transient same-program helper
                    # window that dies before any keystroke makes
                    # this retry capture the ORIGINAL window again.
                    # Every credit then still belongs to the window
                    # the backspaces would reach, so refusing there
                    # cost the user a correction that used to work.
                    # _retry_returned_to_credited answers True only
                    # for that case: one credited window, readable,
                    # and equal to this capture's. Everything else
                    # refuses, including an empty record and credits
                    # split across two windows.
                    #
                    # The scope stays attempt == 1. An utterance
                    # with no retry behaves as it did before this
                    # change even when its words went to two
                    # windows; the focus-drift gate above stays the
                    # only control there. The letter buffer is
                    # deliberately not read: its branch clears the
                    # buffer and sends no backspaces, so it cannot
                    # delete text in the wrong window.
                    if attempt == 1:
                        held_credit = (
                            self.clipboard.accumulated_paste_chars
                            or self.clipboard.accumulated_paste_clusters
                        )
                        back_at_credited = self._retry_returned_to_credited(
                            context,
                        )
                        if held_credit and not back_at_credited:
                            self._retry_rebound_target = True

                # Get Strategy from Router. The text length feeds the
                # Unicode-vs-Standard branch (wh-606yk).
                strategy = self.router.get_strategy(context, insertion_string)

                # wh-fz7j.4: clear the per-instance write-seq buffer so a stale
                # value from a previous insert that did NOT call _safe_copy this
                # time around cannot leak into the deferred-restore baseline.
                # Any clipboard write the strategy makes via _safe_copy will
                # repopulate the value before we read it below.
                self.clipboard.last_clipboard_write_seq = None

                # Execute Strategy
                result = strategy.insert(insertion_string, context, request_id, options)

                # wh-lost-word-neighbour-paths.1.5: record which
                # window took this word, beside the characters
                # ClipboardOperations credits for it.
                self._record_credited_target(context, result)

                # Forward dirty signal to the utterance manager (wh-4z4g9).
                # Done unconditionally on result.clipboard_dirty -- a failed
                # clipboard paste can still leave dictated text on the
                # clipboard, so the manager must restore in that case too.
                #
                # wh-fz7j.2: pass the post-write seq captured inside _safe_copy
                # so the deferred-restore ownership baseline reflects the
                # actual post-write seq, not the seq read at this dirty-mark
                # callsite (which may already have advanced if the user
                # manually copied between the strategy's clipboard write and
                # this point).
                if result.clipboard_dirty:
                    self.utterance_manager.mark_clipboard_dirty(
                        write_seq=self.clipboard.last_clipboard_write_seq
                    )
                    # _last_paste_time feeds wrap_or_insert's "recent paste"
                    # heuristic. Only an actual clipboard write counts; a
                    # Unicode SendInput delivery should not look like a paste
                    # to that heuristic.
                    self.utterance_manager._last_paste_time = time.time()

                # wh-paste-target-window-vanished.1.2: does a retry
                # supersede this attempt? Decided here, once, above the
                # per-attempt bookkeeping, and read again by the retry
                # decision below, so the two can never disagree about
                # which attempt ends the loop.
                #
                # wh-lost-word-neighbour-paths: the strategy allow-list
                # is no longer the thing that proves the attempt was
                # empty -- result.delivered_nothing is, and every
                # strategy sets it per branch. The allow-list stays as
                # the second guard, so a strategy that sets the field
                # wrongly still cannot earn a retry.
                will_retry = (
                    attempt == 0
                    and not result.success
                    and result.delivered_nothing
                    and result.target_window_gone
                    and isinstance(
                        strategy,
                        # The OUTER types the router returns. It never
                        # returns VerifiedUnicodeStrategy: the short-text
                        # branch returns the UnicodeFirstStrategy
                        # composite that wraps it. FlutterStrategy is on
                        # this list by inheritance -- it subclasses
                        # StandardStrategy with an empty body, so it runs
                        # the same insert and computes
                        # delivered_nothing the same way.
                        (
                            UnicodeFirstStrategy,
                            StandardStrategy,
                            ClipboardOnlyStrategy,
                        ),
                    )
                )

                # Track strategy type for retraction gating.
                #
                # wh-lost-word-neighbour-paths: an attempt that PROVED
                # it delivered nothing wrote no characters into any
                # window, so it has nothing for a retraction to walk
                # back and no reason to close the gate on the rest of
                # the utterance. Words a counter-crediting strategy
                # delivered earlier in the same utterance stay
                # retractable. "Not proven" keeps the old behaviour,
                # because an attempt that may have pasted must still
                # block.
                #
                # wh-lost-word-neighbour-paths.1.2: EMPTY IS NOT ENOUGH
                # -- the attempt's window must also be proven GONE.
                # The remembered target moved to the newly focused
                # control a few lines above this, BEFORE the routing
                # decision, so an empty attempt leaves the remembered
                # window naming a window this insert never wrote to
                # while the characters on the counter were credited in
                # the PREVIOUS window. If that newly bound window is
                # still ALIVE, retract's focus-drift gate compares the
                # remembered window against the foreground, both name
                # it, the gate passes, and the backspaces delete text
                # the program did not write -- the same hazard the
                # RejectedInsertionStrategy branch below spells out,
                # reached through this route. A window proven gone
                # cannot hold the foreground, so there the focus-drift
                # gate refuses the re-bound case by itself, and the
                # exemption only has to stop the per-utterance gate
                # staying shut for a LATER word that a counter-
                # crediting strategy delivers into a live window of its
                # own. Never re-widen this to delivered_nothing alone:
                # emptiness says nothing about WHICH window the attempt
                # bound.
                if isinstance(strategy, SimplePasteStrategy):
                    if not (
                        result.delivered_nothing and result.target_window_gone
                    ):
                        self._used_simple_paste = True
                elif isinstance(strategy, ClipboardOnlyStrategy):
                    # wh-9weum Phase 1 (wh-0ci9n) /
                    # wh-soft-allow-verdict-tier: ClipboardOnly is the
                    # silent-paste tier for soft-allow accepts -- targets
                    # the user has explicitly approved via the
                    # three-strikes grant prompt. The strategy still does
                    # NOT advance accumulated_paste_chars: even though the
                    # user has approved the target, ClipboardOnly cannot
                    # verify the paste landed (the target does not surface
                    # UIA TextPattern), so a later retract over the same
                    # utterance would walk back the wrong span if the
                    # counter advanced here. Reuse the simple_paste
                    # retraction gate -- both strategies share the
                    # "we cannot prove what landed" property.
                    #
                    # wh-paste-target-window-vanished.1.2: except when a
                    # retry is about to supersede this attempt. The gate
                    # is per-utterance and nothing clears it inside an
                    # utterance, so an attempt that fired no keystroke
                    # and credited nothing would otherwise block the
                    # retraction of a word the NEXT attempt delivers
                    # through a strategy that does credit the counter.
                    # The attempt that ends the loop governs the gate;
                    # every other piece of bookkeeping in this branch
                    # still runs for both attempts.
                    #
                    # wh-lost-word-neighbour-paths: both conditions
                    # apply. A superseded attempt is not the one that
                    # governs the gate, and an attempt that proved it
                    # delivered nothing has nothing to protect -- the
                    # attempt that ENDS the loop can still be an empty
                    # one, and it must not block the retraction of a
                    # word another strategy delivered.
                    #
                    # wh-lost-word-neighbour-paths.1.2: and the empty
                    # attempt's window must be proven GONE as well.
                    # This is the route the reported defect travels:
                    # ClipboardOnlyStrategy sets delivered_nothing on
                    # its pre-send refusal while target_window_gone
                    # stays False for a live window that merely lost
                    # the foreground, and the remembered target already
                    # names that live window. See the full reasoning
                    # above the SimplePasteStrategy branch.
                    if not will_retry and not (
                        result.delivered_nothing and result.target_window_gone
                    ):
                        self._used_simple_paste = True
                    # Review wh-kox5.3: invalidate the shadow buffer because
                    # the soft-paste may have changed the target's content
                    # and the buffer's preceding-chars mirror is no longer
                    # reliable. If a later Standard or VerifiedUnicode call
                    # in the same utterance reads the buffer's get_context()
                    # without re-syncing, TextPerfector composes against
                    # stale preceding text and produces wrong spacing or
                    # capitalization. The buffer's normal invalidate path
                    # only fires on user mouse/keyboard input; voice-only
                    # routing through ClipboardOnly never reaches it. Force
                    # the invalidation here so the next compose re-syncs
                    # via UIA before any TextPerfector pass.
                    self.buffer_manager.invalidate()
                elif isinstance(strategy, RejectedInsertionStrategy):
                    # wh-paste-when-unverified.3.4: a rejected insert
                    # delivered NOTHING, and that is exactly why the
                    # retraction gate has to close. The remembered window
                    # moved to the newly focused control a few lines above
                    # this, BEFORE the routing decision, so by now it names
                    # a window this insert never wrote to. The retraction's
                    # own focus check compares the remembered window against
                    # the foreground and would therefore pass, and the
                    # backspaces would delete text the program did not
                    # write -- characters credited in the PREVIOUS window
                    # are still on the counter. Blocking the retraction
                    # costs a correction spoken after a rejected insert; a
                    # rejected insert put nothing on screen for that
                    # correction to walk back.
                    #
                    # Deliberately set for EVERY RejectedInsertionStrategy,
                    # not only the empty-identity silent drop: the elevated-
                    # window refusal returns the same object and carries the
                    # same hazard. No buffer_manager.invalidate() here --
                    # nothing was written, so the shadow buffer's mirror of
                    # the target is still whatever it was.
                    self._used_simple_paste = True

                # wh-paste-when-unverified.2: record WHY this insertion did
                # not deliver, at the one place the handler reads the
                # result. A pre-send refusal is a deliberate no-op; every
                # other non-delivery is a fault. end_utterance needs the
                # difference to pick its log level.
                self._last_insert_was_rejected = result.was_rejected

                # wh-paste-target-window-vanished: the retry decision.
                # Two strategies set target_window_gone, each on a
                # branch that fired no keystroke: ClipboardOnlyStrategy
                # on its soft-paste refusal, and ClipboardFallbackStrategy
                # on its preflight refusal (wh-lost-word-neighbour-paths).
                # The second one is what carries the answer out of both
                # default dictation paths, because StandardStrategy
                # returns its fallback attempt's result and
                # UnicodeFirstStrategy returns StandardStrategy's. A
                # strategy that sends first and verifies afterwards could
                # already have delivered the word, so a retry there could
                # type it twice; the isinstance check is what keeps the
                # retry off those paths even if one of them ever sets the
                # field.
                #
                # wh-paste-target-window-vanished.1.2: the condition now
                # lives in will_retry, computed above the bookkeeping
                # that has to know the same answer.
                if not will_retry:
                    break
                logger.info(
                    "%s: %s refused before any keystroke because the "
                    "captured window no longer exists; capturing the "
                    "target once more (program=%s).",
                    action_name, type(strategy).__name__,
                    captured_process_name or "unreadable",
                )
                attempt += 1

            if result.success:
                # wh-zndq: a pre-send rejection (RejectedInsertionStrategy)
                # also returns success=True so the caller's Future resolves
                # without traceback noise. Emit PATH_INSERT_REJECTED so
                # downstream paths can distinguish a delivered insertion
                # from an intentional silent no-op.
                if result.was_rejected:
                    self.response.send_success(
                        request_id,
                        action_name,
                        ResponseHandler.PATH_INSERT_REJECTED,
                        rejected_reason=result.rejected_reason,
                    )
                else:
                    # wh-9weum Phase 1 (wh-pc28): include retry_outcome
                    # so the logic process can branch on the verified vs.
                    # unverified status of a ClipboardOnlyStrategy result.
                    # Non-ClipboardOnly strategies leave the field as
                    # 'n/a' which the logic side treats as a no-op
                    # signal (Phase 4 click counter ignores 'n/a').
                    self.response.send_success(
                        request_id,
                        action_name,
                        ResponseHandler.PATH_INSERT_VERIFIED,
                        retry_outcome=result.retry_outcome,
                    )
            else:
                logger.warning(
                    "Strategy %s returned success=False for %s",
                    type(strategy).__name__, action_name,
                )
                self.response.send_error(
                    request_id,
                    action_name,
                    "strategy returned False",
                )
            # wh-review-pattern-fixes.11: report delivery, not the Schema A
            # status. A rejected result has success=True (the Future
            # resolves cleanly) but no text landed.
            return result.success and not result.was_rejected

        except Exception as e:
            logger.error(f"Error in {action_name}: {e}", exc_info=True)
            # Schema A error response so the caller Future resolves and the
            # demuxer does not log unknown/timed-out (wh-lla5d).
            self.response.send_error(
                request_id,
                action_name,
                str(e),
            )
            return False

    def verbatim_insert_text(
        self,
        text: str,
        request_id: Optional[str] = None,
        captured_target: Optional[CapturedTarget] = None,
    ) -> bool:
        """Insert ``text`` verbatim through the strategy router (wh-iti5).

        Same routing as ``intelligent_insert_text`` -- short text goes
        through Unicode SendInput, long text falls back to clipboard --
        but the strategy receives ``InsertionOptions(mode=VERBATIM)`` so
        no TextPerfector pass runs and no prefix space is added.

        Used by callers that already composed the final text:
        ``wrap_or_insert``'s selection-wrap branch (the wrapped string
        is the final text) and ``transform_selection`` (the transformed
        text is the final text). Without verbatim mode those callers
        would re-run TextPerfector on already-composed text, prepending
        leading spaces or fighting the casing the user asked for.

        Does NOT add its own ``clipboard_context`` wrap. Callers are
        responsible for clipboard preservation around the call --
        ``wrap_or_insert`` and ``transform_selection`` already wrap the
        whole sequence so a nested wrap would double-restore. When the
        chosen strategy actually wrote the clipboard,
        ``mark_clipboard_dirty()`` runs as a safety net so
        ``end_utterance`` restores even if a synchronous restore racy.

        Returns the delivery outcome (wh-review-pattern-fixes.11): True
        only when the strategy delivered the text. A pre-send rejection
        returns False even though its Schema A response is a success,
        so ``transform_selection`` reports a paste failure instead of a
        false "Successfully transformed". When ``request_id`` is not
        None, also emits a Schema A response so the caller's Future
        resolves; pass ``None`` to suppress emission when the caller
        owns its own response.

        wh-review-pattern-fixes.45: ``captured_target`` names the
        control the caller copied the selection from. Both selection
        callers pass it. ``_execute_insert_with_ack`` proves it holds
        the foreground before it routes, and ``SimplePasteStrategy``
        forwards it to ``verified_paste`` for the second proof
        immediately before Ctrl+V. Leave it None only when there is no
        captured source control to compare against.
        """
        return self._execute_insert_with_ack(
            text,
            request_id,
            options=InsertionOptions(
                mode=InsertionMode.VERBATIM,
                captured_target_control=(
                    captured_target.control if captured_target else None
                ),
                captured_target_hwnd=(
                    captured_target.hwnd if captured_target else None
                ),
            ),
            action_name="verbatim_insert_text",
        )

    # ========================================================================
    # PUBLIC API - Selection Transformations
    # ========================================================================

    def transform_selection(self, transformation_type: str, request_id: Optional[str] = None):
        """Transform selected text with wrapping or case conversion.

        Process:
        1. Copy selected text (or select all if no selection)
        2. Apply transformation
        3. Paste back transformed text
        4. Restore clipboard

        Args:
            transformation_type: Type of transformation (quote, snake_case, etc.)
            request_id: Optional request ID for response tracking
        """
        import pyperclip
        import time

        logger.info(f"Transforming selection with: {transformation_type}")
        success = False
        message = ""

        try:
            # Capture Context for Flutter detection
            context = capture_context()
            _stop_command_on_failed_focus_read(context, "transform_selection")
            focused_control = context.focused_control
            is_flutter = context.is_flutter
            # wh-review-pattern-fixes.45: record the control the
            # selection is about to be copied from, and the top-level
            # window it belongs to. The paste-back below travels through
            # verbatim_insert_text, which captures a FRESH context and
            # routes against whatever control that returns. Without this
            # pair the transformed text lands in whatever window holds
            # the foreground when the paste fires.
            captured_target = CapturedTarget(
                control=focused_control,
                hwnd=_captured_hwnd_from_control(focused_control),
            )

            with clipboard_context(restore_delay=0.05):
                # wh-review-pattern-fixes.25 (sibling of the
                # wrap_or_insert selection branch): a single letter
                # deferred earlier in this utterance can still sit in
                # _letter_buffer. This method copies and replaces the
                # selection through verbatim_insert_text, which never
                # flushes the buffer, so the transform would mutate a
                # selection the letter should already have replaced.
                # Flush BEFORE the sentinel write and the Ctrl+C copy.
                # After a delivered flush the letter has consumed the
                # selection, so the sentinel poll below finds no
                # selection and returns without a transform -- the
                # natural re-evaluation.
                if self._letter_buffer:
                    if not self._flush_letter_buffer():
                        # Fail closed (finding .23 design): do not copy
                        # or mutate the selection; the finally block
                        # emits the one failure response.
                        message = (
                            "Letter buffer flush failed; selection not "
                            "touched"
                        )
                        logger.warning(f"transform_selection: {message}")
                        success = False
                        return

                # wh-r7al.2: this branch writes the system clipboard
                # (sentinel below, then verified_paste after the
                # transformation). The inner clipboard_context restores
                # the saved value at exit, but its restore step catches
                # exceptions and only logs a warning -- if the restore
                # fails (clipboard lock contention, pyperclip raises),
                # the dictated/transformed text would stay on the user's
                # clipboard with no later recovery. Mark the utterance
                # dirty up front so end_utterance restores the
                # utterance-start clipboard as a safety net.
                self.utterance_manager.mark_clipboard_dirty()

                # Get selected text.
                # wh-fz7j.4: route through _safe_copy so last_clipboard_write_seq
                # tracks the sentinel write -- if the no-selection early
                # return fires, the deferred-restore ownership check still
                # has a valid baseline.
                # wh-fz7j.5: bail out if the sentinel write itself fails;
                # otherwise an existing clipboard value would be treated as
                # selected text and transformed.
                sentinel = f"__SENTINEL__{time.time()}"
                if not self.clipboard._safe_copy(sentinel):
                    message = "Could not write clipboard sentinel"
                    logger.error(message)
                    success = False
                    return
                # Forward the sentinel's seq to the utterance manager so
                # _last_wheelhouse_seq reflects the latest WheelHouse write
                # even if the no-selection early return fires below.
                self.utterance_manager.mark_clipboard_dirty(
                    write_seq=self.clipboard.last_clipboard_write_seq
                )

                # Copy selection (Flutter-aware)
                if is_flutter and focused_control and focused_control.Exists(0, 0):
                    focused_control.SendKeys('{Ctrl}c')
                else:
                    # wh-review-pattern-fixes.35: the copy must be
                    # verified. press_keys discards the SendInput
                    # count, and an undelivered Ctrl+C leaves the
                    # sentinel unchanged -- indistinguishable from a
                    # genuine empty selection, so the method would
                    # report the false result "No text selected". Fail
                    # closed on a short delivery: release a
                    # possibly-held Ctrl (down accepted, up dropped --
                    # wh-eolas.2.5 shape) and report a copy failure.
                    ok, accepted, expected = verified_press_keys(
                        'ctrl', 'c'
                    )
                    if not ok:
                        _send_modifier_keyups(('ctrl',))
                        message = (
                            "Copy shortcut delivery failed; selection "
                            "not touched"
                        )
                        logger.error(
                            "transform_selection: Ctrl+C SendInput "
                            "short delivery (sent %d/%d); %s.",
                            accepted, expected, message,
                        )
                        success = False
                        return

                # Poll clipboard until it changes from sentinel (or timeout)
                start_time = time.time()
                timeout = self.clipboard.clipboard_verification_timeout
                selected_text = sentinel

                while selected_text == sentinel:
                    time.sleep(0.005)  # 5ms polling interval
                    selected_text = pyperclip.paste()
                    if time.time() - start_time > timeout:
                        break  # Timeout - no selection detected

                # If no selection, do nothing
                if selected_text == sentinel:
                    message = "No text selected to transform"
                    logger.info(message)
                    success = False
                    return

                # Apply transformation
                transformed_text = self.selection_transformer.apply_transformation(
                    selected_text,
                    transformation_type
                )

                if transformed_text is None:
                    message = f"Unknown transformation type: {transformation_type}"
                    logger.error(message)
                    success = False
                    return

                # Paste back transformed text. wh-iti5: route through
                # verbatim_insert_text so the strategy router selects the
                # right delivery for the active target -- the terminal
                # dictation editor receives the transformed text via IPC
                # (no clipboard race), normal short text goes via Unicode
                # SendInput, long text via clipboard. Pass request_id=None
                # so verbatim_insert_text suppresses its Schema A emission;
                # transform_selection emits its own legacy-format response
                # below.
                # wh-review-pattern-fixes.45: carry the capture-time
                # control and window so the paste-back can only land in
                # the field the selection came from.
                success = self.verbatim_insert_text(
                    transformed_text, request_id=None,
                    captured_target=captured_target,
                )

                if success:
                    message = f"Successfully transformed selection with {transformation_type}"
                    logger.info(message)
                elif self.last_insert_refused_for_focus_drift:
                    # wh-review-pattern-fixes.45: name the real reason.
                    # Nothing was sent, so the user's selection is still
                    # on screen and their text is unchanged.
                    message = (
                        "The window changed after the copy; the "
                        "selection was not transformed"
                    )
                    logger.error(message)
                else:
                    message = "Failed to paste transformed text"
                    logger.error(message)

        except Exception as e:
            message = f"Error transforming selection: {e}"
            logger.error(message, exc_info=True)
            success = False
        finally:
            # Invalidate buffer since text changed
            self.buffer_manager.invalidate()

            # Send response if request_id provided
            if request_id:
                self.response_queue.put({
                    'type': 'response',
                    'request_id': request_id,
                    'success': success,
                    'message': message
                })

    def wrap_or_insert(self, left_fence: str, right_fence: str, text: str = "", request_id: Optional[str] = None):
        """Intelligently wrap selection, insert wrapped text, or insert empty delimiters.
        
        Three-tier logic:
        1. Check if text is selected (sentinel check) → wrap selection if exists
        2. If no selection but captured text exists → insert wrapped text
        3. If no selection and no text → insert empty delimiters with cursor between
        
        Args:
            left_fence: Opening delimiter (e.g., "(", "[", "<", "{", "'", '"')
            right_fence: Closing delimiter (e.g., ")", "]", ">", "}", "'", '"')
            text: Optional captured text from pattern (empty string if none)
            request_id: Optional request ID for response tracking
        """
        import pyperclip
        import time
        
        logger.info(f"wrap_or_insert: fences={left_fence}{right_fence}, text='{redact_transcript(text) if text else ''}'")
        
        # Strip text to check if we have actual content
        text_stripped = text.strip() if text else ""
        
        try:
            # Capture Context for Flutter detection
            context = capture_context()
            _stop_command_on_failed_focus_read(context, "wrap_or_insert")
            focused_control = context.focused_control
            is_flutter = context.is_flutter

            with clipboard_context(restore_delay=0.05):
                # If no captured text AND no recent paste activity, check for selection
                # (User said "quote" without text, may have selected text manually)
                # Skip selection check if text was pasted recently (within 5 seconds)
                # This handles both mid-utterance wraps AND VS Code auto-select between utterances
                time_since_last_paste = time.time() - self.utterance_manager._last_paste_time
                check_selection = not text_stripped and time_since_last_paste > 5.0
                logger.info(f"[WRAP_CHECK] text_stripped={bool(text_stripped)}, time_since_last_paste={time_since_last_paste:.1f}s, check_selection={check_selection}")

                # wh-review-pattern-fixes.25: a single letter deferred
                # earlier in this utterance can still sit in
                # _letter_buffer (the router classifies it as DICTATE
                # and intelligent_insert_text buffers it). The selection
                # branch below copies and replaces the selection through
                # verbatim_insert_text, which never flushes the buffer,
                # so the wrap would land AHEAD of the earlier letter and
                # wrap a selection the letter should already have
                # replaced. Flush BEFORE any selection capture. The
                # other branches flush inside intelligent_insert_text.
                if check_selection and self._letter_buffer:
                    if not self._flush_letter_buffer():
                        # Fail closed (finding .23 design): the letters
                        # did not land and must not be replayed blind.
                        # Do not copy or mutate the selection, leave the
                        # caret alone, emit the one Schema A error this
                        # request_id is owed.
                        logger.warning(
                            "wrap_or_insert: letter-buffer flush failed "
                            "before selection capture; selection not "
                            "touched."
                        )
                        self.response.send_error(
                            request_id,
                            "wrap_or_insert",
                            "letter buffer flush failed; selection not touched",
                        )
                        return
                    # The delivered flush letter replaced the selection,
                    # so the correct branch is now the empty-delimiter
                    # insertion path (Priority 2), not the wrap.
                    check_selection = False

                if check_selection:
                    # wh-r7al.2: this branch writes the system clipboard
                    # via Ctrl+C (selection capture) and verified_paste
                    # (wrapped text). The inner clipboard_context
                    # restores at exit, but its restore step swallows
                    # exceptions and only logs a warning -- if the
                    # restore fails (clipboard lock contention), the
                    # captured selection or wrapped text would stay on
                    # the user's clipboard with no later recovery. Mark
                    # the utterance dirty up front so end_utterance
                    # restores the utterance-start clipboard as a
                    # safety net.
                    self.utterance_manager.mark_clipboard_dirty()

                    # wh-review-pattern-fixes.31: probe with a unique
                    # sentinel (the pattern transform_selection and
                    # capture_selected_text already use), NOT a
                    # comparison against the pre-copy clipboard value.
                    # When the selected text equals the current
                    # clipboard content, a successful Ctrl+C changes
                    # nothing; the original-value comparison then timed
                    # out, concluded "no selection", and Priority 2
                    # inserted empty fences OVER the still-active
                    # selection. The sentinel is unique per call, so a
                    # successful copy always changes the polled value.
                    sentinel = f"__SENTINEL__{time.time()}"
                    if not self.clipboard._safe_copy(sentinel):
                        # Fail closed: without the sentinel the probe
                        # cannot distinguish selection from
                        # no-selection, and falling through would
                        # insert empty fences over a possibly-active
                        # selection -- the exact data loss this branch
                        # exists to avoid.
                        logger.error(
                            "wrap_or_insert: clipboard sentinel write "
                            "failed; selection state unknown, nothing "
                            "touched."
                        )
                        self.response.send_error(
                            request_id,
                            "wrap_or_insert",
                            "could not write clipboard sentinel; "
                            "selection not touched",
                        )
                        return
                    # Forward the sentinel's seq so the deferred-restore
                    # ownership baseline reflects the latest WheelHouse
                    # write (same as transform_selection, wh-fz7j.4).
                    self.utterance_manager.mark_clipboard_dirty(
                        write_seq=self.clipboard.last_clipboard_write_seq
                    )

                    # Send Ctrl+C to copy any selection
                    if is_flutter and focused_control and focused_control.Exists(0, 0):
                        focused_control.SendKeys('{Ctrl}c')
                    else:
                        # wh-review-pattern-fixes.35: verified copy.
                        # An undelivered Ctrl+C leaves the sentinel
                        # unchanged, the poll times out, and Priority 2
                        # inserts empty fences OVER the still-active
                        # selection -- the exact data loss this branch
                        # exists to avoid. Fail closed on a short
                        # delivery: release a possibly-held Ctrl and
                        # emit the one Schema A error this request_id
                        # is owed (same shape as the sentinel-write
                        # failure above).
                        ok, accepted, expected = verified_press_keys(
                            'ctrl', 'c'
                        )
                        if not ok:
                            _send_modifier_keyups(('ctrl',))
                            logger.error(
                                "wrap_or_insert: Ctrl+C SendInput "
                                "short delivery (sent %d/%d); "
                                "selection state unknown, nothing "
                                "touched.",
                                accepted, expected,
                            )
                            self.response.send_error(
                                request_id,
                                "wrap_or_insert",
                                "copy shortcut delivery failed; "
                                "selection not touched",
                            )
                            return

                    # Poll clipboard until it changes from the sentinel
                    # (or timeout -> no selection)
                    start_time = time.time()
                    timeout = self.clipboard.clipboard_verification_timeout
                    current_clipboard = sentinel
                    poll_count = 0

                    while current_clipboard == sentinel:
                        time.sleep(0.005)  # 5ms polling interval
                        current_clipboard = pyperclip.paste()
                        poll_count += 1
                        if time.time() - start_time > timeout:
                            break  # Timeout - no selection detected

                    if current_clipboard != sentinel:
                        # Selection found - wrap it. wh-iti5: route through
                        # verbatim_insert_text so the strategy router
                        # handles delivery (terminal-editor IPC when the
                        # editor is active, Unicode SendInput for short
                        # text in normal apps, clipboard fallback for
                        # long text). The previous direct verified_paste
                        # call wrote the wrapped text to the system
                        # clipboard and raced the inner clipboard_context
                        # restore on Qt event-loop apps -- including the
                        # terminal dictation editor itself, which would
                        # paste the restored (unrelated) clipboard
                        # content instead of the wrapped selection.
                        # verbatim_insert_text emits the Schema A
                        # response; no extra emission needed here.
                        #
                        # wh-review-pattern-fixes.45: carry the control
                        # the selection was copied from. Without it
                        # verbatim_insert_text captures a fresh context
                        # and wraps whatever the foreground window has
                        # selected now, which is the same defect
                        # transform_selection had.
                        logger.info(f"Wrapping selected text: '{redact_transcript(current_clipboard[:50])}'...")
                        wrapped = f"{left_fence}{current_clipboard}{right_fence}"
                        self.verbatim_insert_text(
                            wrapped, request_id,
                            captured_target=CapturedTarget(
                                control=focused_control,
                                hwnd=_captured_hwnd_from_control(focused_control),
                            ),
                        )
                        return

                # Priority 1: If captured text exists, insert wrapped text
                if text_stripped:
                    logger.info(f"Inserting wrapped captured text: '{redact_transcript(text_stripped)}'")
                    wrapped = f"{left_fence}{text_stripped}{right_fence}"
                    # intelligent_insert_text owns the Schema A response for
                    # this request_id (wh-lla5d).
                    self.intelligent_insert_text(wrapped, request_id)
                    return

                # Priority 2: No text → insert empty delimiters + position cursor
                logger.info(f"Inserting empty delimiters: {left_fence}{right_fence}")
                empty_delimiters = f"{left_fence}{right_fence}"
                # Pass request_id=None so the nested call does not race to
                # resolve the caller's Future before we finish positioning
                # the cursor. wrap_or_insert owns the response for this path
                # and emits below (wh-d43oi).
                # wh-review-pattern-fixes.16: defer_single_letter=False --
                # a manually authored pattern can make the concatenated
                # fences a single alphabetic letter, and the letter-buffer
                # branch would insert nothing physically, return True, and
                # let the left-arrow press below shift the caret before
                # the deferred flush lands the letter.
                delivered = self.intelligent_insert_text(
                    empty_delimiters, request_id=None,
                    defer_single_letter=False,
                )

                # wh-review-pattern-fixes.11: only move the caret and
                # claim a verified insert when the nested insertion
                # actually delivered the delimiters. On a strategy
                # failure, a handled exception, or a pre-send rejection,
                # no text landed -- a left-arrow press would move the
                # user's existing caret and the verified success would
                # be false.
                # wh-review-pattern-fixes.23: delivered is also False
                # when a preceding letter-buffer flush failed (the
                # delimiters are then never attempted), so this gate
                # covers the flush outcome too.
                if not delivered:
                    logger.warning(
                        "wrap_or_insert: empty delimiter insertion was "
                        "not delivered (insertion or preceding "
                        "letter-buffer flush failed); caret not moved."
                    )
                    self.response.send_error(
                        request_id,
                        "wrap_or_insert",
                        "empty delimiter insertion not delivered",
                    )
                    return

                # Move cursor left to position between delimiters.
                # wh-review-pattern-fixes.37: the press must be
                # acknowledged. press_key_action sends through
                # fire-and-forget press_keys, discards the SendInput
                # count, catches every exception, and returns None, so
                # a dropped Left produced delimiters on screen with the
                # caret AFTER the closing delimiter while this path
                # still reported PATH_INSERT_VERIFIED. The next
                # dictated word then landed outside the delimiters
                # although the voice feedback said the command
                # completed. press_key_verified reports the SendInput
                # outcome (wh-review-pattern-fixes.32 (c)).
                time.sleep(0.05)  # Small delay to ensure text is inserted
                left_result = self.press_key_verified("left", repeat=1)

                if not (left_result and left_result.get("success")):
                    # The delimiters ARE on screen; only the caret move
                    # failed. Report that specific partial outcome. No
                    # rollback: a backspace over text the caret may no
                    # longer sit behind would destroy the user's own
                    # content, and the caret position is exactly what
                    # this branch just failed to establish.
                    logger.error(
                        "wrap_or_insert: empty delimiters were inserted "
                        "but the caret move was not acknowledged; the "
                        "caret is not between them."
                    )
                    self.response.send_error(
                        request_id,
                        "wrap_or_insert",
                        "delimiters inserted but the caret was not "
                        "positioned between them",
                    )
                    return

                self.response.send_success(
                    request_id,
                    "wrap_or_insert",
                    ResponseHandler.PATH_INSERT_VERIFIED,
                )

        except Exception as e:
            logger.error(f"Error in wrap_or_insert: {e}", exc_info=True)
            self.response.send_error(
                request_id,
                "wrap_or_insert",
                str(e),
            )

    # ========================================================================
    # PUBLIC API - Low-Level Input
    # ========================================================================

    def select_phrase(self, phrase: str, request_id: Optional[str] = None):
        """Select the first match of a spoken phrase in the focused control.

        wh-spoken-phrase-select.2. The user speaks words, and this method
        selects the first place those words appear in the document. The
        search runs inside the target application through UI Automation,
        so the document text never crosses a process boundary.

        The match is exact apart from letter case. See
        ui/uia_phrase_select.py for why a tolerant match is not possible
        in Word, and for the measured cost of the search and the select.

        Five of the six outcomes leave the screen unchanged. Each one
        shows the user a Windows notification, which a screen reader
        announces. ui/phrase_select_notice.py holds the wording and the
        reason for that choice (wh-spoken-phrase-select.3).

        Args:
            phrase: The words to find.
            request_id: Present so the dispatcher can bind the call. This
                method is not in _HANDLES_OWN_RESPONSE, so the generic
                dispatcher sends the response.

        Returns:
            One of the PHRASE_ constants in ui/uia_phrase_select.py.
        """
        try:
            # Read only the focused control, not the whole context.
            # capture_context() also reads the class name, the process
            # id and the top level window, which this command discards,
            # and it logs an ERROR when one of them raises. Every ERROR
            # record shows the user a notification box, so a select that
            # succeeds could still show the user an error.
            # wh-spoken-phrase-select.7.1.
            focused_control = read_focused_control()
            outcome, _matched = select_phrase_in_control(
                focused_control, phrase
            )
        except Exception as e:
            # The phrase never appears in this line. The words a person
            # speaks are theirs (wh-797.17).
            logger.warning(
                "select_phrase failed before the search: %s", type(e).__name__
            )
            outcome = PHRASE_FAILED
        else:
            logger.info("select_phrase outcome: %s", outcome)

        # The report never changes the answer. A broken notifier must
        # not turn a select that worked into a select that failed.
        try:
            notify_outcome(outcome, phrase, get_notifier_worker())
        except Exception as e:
            logger.warning(
                "select_phrase could not show the outcome: %s",
                type(e).__name__,
            )
        return outcome

    def raw_insert_text(self, text: str, request_id: Optional[str] = None):
        """Insert raw text at cursor by routing through the strategy router.

        Routes through the InsertionRouter with InsertionOptions(VERBATIM)
        so short text in normal apps lands via VerifiedUnicodeStrategy
        (no clipboard write, no race), long text falls through to
        StandardStrategy's clipboard fallback, terminal apps go through
        the editor proxy IPC, and unfocusable targets fall back to
        SimplePasteStrategy. Verbatim mode skips TextPerfector and the
        shadow buffer sync gate so the caller's exact text is delivered.

        wh-fsov0: this replaces the prior single-strategy clipboard paste
        wrapped in a fixed clipboard_context(restore_delay=0.05). The
        wrapper restored the user's clipboard 50 ms after the keystroke,
        which the destination application sometimes consumed AFTER the
        restore -- leaving the original clipboard content on screen
        instead of the dictated text. The deferred-restore policy from
        wh-d0lr1 (PendingRestore in UtteranceClipboardManager) replaces
        that mechanism for clipboard-backed paths: end_utterance schedules
        a deferred restore that fires after restore_deferral_s with an
        ownership check.

        On strategy failure, raises PasteFailedError so the input_proc
        dispatcher's except branch produces a Schema A error response
        for callers that supplied request_id. raw_insert_text is NOT in
        _HANDLES_OWN_RESPONSE: fire-and-forget callers (no request_id)
        get the exception logged with no response sent.

        Note on outside-of-utterance use: the deferred-restore path on
        end_utterance is the manager's mechanism for clipboard-backed
        delivery; if no utterance is active when raw_insert_text fires
        (rare in production voice flow -- every voice command runs
        inside an utterance), the clipboard write happens but no
        deferred restore is scheduled. The mark_clipboard_dirty call is
        a no-op without an active utterance. Programmatic callers
        outside the voice path are responsible for their own clipboard
        management.
        """
        context = capture_context()
        if context.focused_control:
            self.window_manager.remember_target(context.focused_control)

        strategy = self.router.get_strategy(context, text)

        # wh-fz7j.4: reset the seq buffer so a stale value from a prior
        # _safe_copy cannot leak into mark_clipboard_dirty if this
        # strategy never writes the clipboard.
        self.clipboard.last_clipboard_write_seq = None

        options = InsertionOptions(mode=InsertionMode.VERBATIM)
        try:
            result = strategy.insert(text, context, request_id, options)
        except Exception:
            # wh-d94c.3: the strategy raised. If it touched the clipboard
            # before raising, last_clipboard_write_seq is non-None and
            # we still owe the manager a dirty mark so the deferred
            # restore can recover at end_utterance. Without this, an
            # exception after a write would leave the dictated text on
            # the user's clipboard with no scheduled restore.
            if self.clipboard.last_clipboard_write_seq is not None:
                self.utterance_manager.mark_clipboard_dirty(
                    write_seq=self.clipboard.last_clipboard_write_seq
                )
                self.utterance_manager._last_paste_time = time.time()
            raise

        # wh-lost-word-neighbour-paths.1.5: same record as the
        # dictation path, so a verbatim word cannot leave the set
        # empty while the counter it credited stands.
        self._record_credited_target(context, result)

        if result.clipboard_dirty:
            self.utterance_manager.mark_clipboard_dirty(
                write_seq=self.clipboard.last_clipboard_write_seq
            )
            self.utterance_manager._last_paste_time = time.time()

        # wh-bkge.1: track strategy type for retraction gating. Mirrors
        # _execute_insert_with_ack at the parallel intelligent_insert_text
        # path. A raw insert routed through SimplePasteStrategy (no
        # focused control fallback) or ClipboardOnlyStrategy (soft
        # fallback) must leave retract fail-closed because neither
        # strategy can verify the paste actually landed.
        # wh-lost-word-neighbour-paths: the same guard as the
        # _execute_insert_with_ack branches. An attempt that proved it
        # put no input in the queue and wrote nothing into the target
        # has nothing for a retraction to walk back, so it must not
        # close the gate on words another strategy delivered. This
        # caller runs one attempt only, so there is no will_retry here.
        #
        # wh-lost-word-neighbour-paths.1.2: and the same narrowing.
        # This caller also remembers the newly focused window BEFORE it
        # routes (a few lines above), so an empty attempt against a
        # window that is still ALIVE leaves the remembered target
        # naming a window nothing was written to while the previous
        # window's characters are still on the counter, and retract's
        # focus-drift gate passes because both sides name the new
        # window. Only a window proven GONE is safe to leave the gate
        # open for; a dead window can never hold the foreground. The
        # full reasoning is above the SimplePasteStrategy branch in
        # _execute_insert_with_ack.
        if isinstance(strategy, SimplePasteStrategy):
            if not (result.delivered_nothing and result.target_window_gone):
                self._used_simple_paste = True
        elif isinstance(strategy, ClipboardOnlyStrategy):
            # wh-9weum Phase 1 (wh-0ci9n): same rationale as the
            # _execute_insert_with_ack branch -- soft-fallback paste
            # poisons retract because the paste's actual landing in
            # the target cannot be confirmed.
            if not (result.delivered_nothing and result.target_window_gone):
                self._used_simple_paste = True
            # Review wh-kox5.3: invalidate the shadow buffer; see the
            # _execute_insert_with_ack branch for the full reasoning.
            self.buffer_manager.invalidate()
        elif isinstance(strategy, RejectedInsertionStrategy):
            # wh-paste-when-unverified.3.4: same rationale as the
            # _execute_insert_with_ack branch. This caller also
            # remembers the newly focused window before it routes, so a
            # rejected insert leaves the remembered window naming a
            # window nothing was written to while the previous window's
            # credited characters are still on the counter; retracting
            # there would delete text the program did not write. No
            # buffer_manager.invalidate() -- a rejected insert wrote
            # nothing, so there is nothing to invalidate.
            self._used_simple_paste = True

        if not result.success:
            raise PasteFailedError(
                f"raw_insert_text failed for text {text[:50]!r}"
            )

    def type_text(self, text: str, **kwargs):
        """Type text character-by-character via SendInput.

        Unlike intelligent_insert_text (clipboard paste with spacing/context
        logic), this sends raw keystrokes. Used by patterns like 'find <text>'
        where text must be typed into a dialog, not pasted.

        Args:
            text: Text to type via SendInput
        """
        # Review wh-review-pattern-fixes.5: the typed characters change
        # the target text and move the caret, and the keyboard listener
        # ignores WheelHouse's own synthetic input while an internal
        # action runs (input_proc._should_emit_keyboard_invalidation
        # returns False), so the action itself must invalidate. Placed
        # before the dispatch, matching hotkey_action, so a partial
        # typing failure still leaves the buffer invalidated.
        self.buffer_manager.invalidate()

        # Decided inside the try, acted on after it: see press_key_action.
        refusal: Optional[str] = None
        try:
            ok, sent, error = type_string_verified(text, caller_notifies=True)
            if not ok:
                refusal = TYPING_REFUSED_MESSAGE.format(
                    sent=sent, total=len(text)
                )
                # The typed text is never logged here, only the counts and
                # the primitive's own short reason.
                logger.warning(
                    "type_text: typing stopped after %d of %d characters (%s)",
                    sent, len(text), error,
                )
        except Exception as e:
            # WARNING, not ERROR: see press_key_action.
            logger.warning(f"Error in type_text: {e}", exc_info=True)
            # ``len`` raises for a text value the primitive could not read
            # either -- ``text=1`` passes the ``if not text`` guard above
            # and reaches this arm -- and a raise here would carry the
            # exception out of the method
            # (wh-keyboard-refusal-notice.1.2). Zero is the honest total
            # when the value has no length: nothing was typed.
            try:
                total = len(text)
            except TypeError:
                total = 0
            refusal = TYPING_REFUSED_MESSAGE.format(sent=0, total=total)
        if refusal is not None:
            self._say_the_keyboard_refused(refusal, source="type text")

    def terminal_editor_cancelled(self, request_id: str = ""):
        """Handle editor cancellation from GUI Process.

        wh-overlay-slow-uia-stale-badges.14.17: routed through the
        session-fenced cleanup so a cancellation delivered late from an
        older session cannot reset the current one.
        """
        self.terminal_editor.cancelled_by_gui(request_id)

    def _settle_overlay_walk(
        self,
        finder,
        foreground,
        *,
        walk_deadline: Optional[float],
        compare_snapshot_id: str,
        trace_id: str,
    ):
        """Read the window until it settles, then compare it with the held list.

        Returns ``(walk, held_summary)``. ``held_summary`` is the stored
        summary of ``compare_snapshot_id`` when the settled read matches it --
        the caller reads "unchanged" from that being non-None -- and ``None``
        every other time, including every failure.

        The read callable performs a full ``overlay_walk`` each time rather
        than a bare tree walk, because the comparison has to be made against
        the SAME processed list the badges are drawn from: the browser DOM
        fold, the owned-popup fold, the taskbar walk and the contiguous
        renumber all run inside ``overlay_walk``, and a raw walk would differ
        from the held snapshot on every read.

        crewcut: two limits are accepted here, both removable later.
        (1) Every read this makes is bounded by the ONE dequeue-anchored
        screen-read deadline this request was given -- ``[click]
        screen_read_timeout_ms`` minus the 250 ms pre-walk margin, 9750 ms at
        the shipped default -- because all reads share that single deadline
        rather than each getting its own. On a slow accessibility provider a
        read can still exceed the bound and produce no list, which ends the
        settle early: the answer is then the last read that DID complete,
        unconfirmed by a matching pair. Removing this means giving each read
        its own budget, which needs a second configured number and a rule for
        how many reads the settle may spend.
        (2) Each read stores its own snapshot, so a settle leaves two or three
        extra UNPINNED entries in the finder's store (capacity 4, TTL 30 s).
        They cannot displace the held snapshot, which is pinned and so immune
        to LRU eviction, and the last insert protects itself. Removing this
        means splitting ``overlay_walk`` into a walk half and a store half,
        which is about 300 lines of shipped fold and renumber logic that three
        other paths share; not worth it for entries the store already reclaims.
        """

        class _SettleWalkFailed(Exception):
            """A read could not produce a list; abort the detector."""

            def __init__(self, walk):
                super().__init__("settle read failed")
                self.walk = walk

        last: list = []

        def _read():
            result = finder.overlay_walk(foreground, deadline=walk_deadline)
            if result.snapshot is None:
                # execution_failed: no list at all. The detector only catches
                # stale-window errors, so anything else propagates -- which is
                # exactly the abort this needs.
                raise _SettleWalkFailed(result)
            last.append(result)
            return result.snapshot.matches

        listener = settle_detector.StructureEventListener(
            foreground.foreground_window
        )
        available = False
        try:
            available = listener.start()
        except Exception:  # noqa: BLE001 - the listener is best-effort
            logger.warning(
                "start_overlay_walk: settle listener failed to start; "
                "degrading to plain two matching reads (trace_id=%s)",
                trace_id, exc_info=True,
            )
        settled = None
        try:
            settled = settle_detector.wait_for_settled_window(
                _read,
                events_between=(
                    listener.any_event_between if available else None
                ),
            )
        except _SettleWalkFailed as failed:
            if not last:
                # Nothing completed, so there is no list to paint and nothing
                # to compare. Hand the failed result back and let the
                # handler's existing execution_failed branch answer.
                return failed.walk, None
            # A later read produced no list, but an earlier one did. Answer
            # with that one rather than with nothing: it is a post-click read,
            # so it is no worse than the single walk this path replaced, and
            # the comparison below still runs -- when it matches the held
            # snapshot the answer is the numbers already on screen, which is
            # the safest outcome available.
            logger.info(
                "start_overlay_walk: a settle read failed (%s) after %d "
                "completed reads; answering with the last completed read "
                "(trace_id=%s)",
                failed.walk.reason, len(last), trace_id,
            )
        finally:
            # stop() unconditionally, not only when start() reported
            # success: a start that registered some handlers and then raised
            # leaves live COM registrations, and stop() is a no-op when there
            # is nothing to release.
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "start_overlay_walk: settle listener failed to stop "
                    "(trace_id=%s)", trace_id, exc_info=True,
                )

        walk = last[-1]
        if settled is not None:
            logger.info(
                "start_overlay_walk: settled=%s after %d reads "
                "(%d voided pairs, %.0f ms, trace_id=%s)",
                settled.settled, settled.read_count, settled.voided_pairs,
                settled.elapsed_ms, trace_id,
            )
        if not compare_snapshot_id or walk.snapshot is None:
            return walk, None
        # wh-overlay-slow-uia-stale-badges.2.2.2: ask for the held list AS THE
        # WINDOW THIS READ RAN AGAINST. ``signature_of`` compares control type
        # id, accessible name and bounding rectangle and carries no window
        # identity, so two windows with the same layout compare as "unchanged"
        # -- a dialog closed and reopened is the ordinary way there. Answering
        # with the held id would then paint the old window's badges over the
        # new one, and the machine's unchanged branch repaints WITHOUT
        # re-pinning, so nothing would rebind the list to the window on screen.
        # ``get_snapshot`` drops the entry and returns None on an identity
        # mismatch, which falls into the held-is-gone path just below and
        # answers with the fresh read; Logic then unpins, pins the new list and
        # paints it. The two other production consumers, show_numbered_overlay
        # and click_snapshot_item, already pass these four fields.
        held = finder.get_snapshot(
            compare_snapshot_id,
            current_foreground_window=foreground.foreground_window,
            current_foreground_pid=foreground.foreground_pid,
            current_foreground_process_name=(
                foreground.foreground_process_name
            ),
            current_foreground_window_creation_time=(
                foreground.foreground_window_creation_time
            ),
        )
        if held is None:
            # The held snapshot expired (TTL) or was unpinned and evicted
            # while the window settled. There is nothing to compare against,
            # so the fresh read is the answer.
            logger.info(
                "start_overlay_walk: the held snapshot %s is gone; "
                "answering with the fresh read (trace_id=%s)",
                compare_snapshot_id, trace_id,
            )
            return walk, None
        if settle_detector.signature_of(
            held.matches
        ) != settle_detector.signature_of(walk.snapshot.matches):
            return walk, None
        held_summary = finder.get_summary(compare_snapshot_id)
        if held_summary is None:
            # The lists match but the summary is gone, so the id cannot be
            # answered with. Fall back to the fresh read rather than send an
            # id with no list behind it.
            return walk, None
        return walk, held_summary

    def start_overlay_walk(
        self,
        scope: str = "focused_window",
        overlay_session_id: int = 0,
        paint_generation: int = 0,
        trace_id: str = "",
        request_id: Optional[str] = None,
        command_dequeue_monotonic: Optional[float] = None,
        settle: bool = False,
        compare_snapshot_id: str = "",
        **kwargs,
    ) -> None:
        """Walk the focused window from scratch for the numbered overlay (wh-n29v.37).

        ``start_overlay_walk`` is the standalone "show numbers" build request:
        Logic dispatches it when the overlay state machine needs a FRESH walk
        of the focused window (no prior ``click_element`` request to reuse).
        The Input process owns the walk:

          1. Gate on the validated overlay config: short-circuit (no walk) when
             ``overlay_enabled_effective`` is False -- the same two-sided gate
             the by-name click path uses (Logic also gates, but the Input side
             defends against a stale/racing request). Defence-in-depth.
          2. Snapshot the foreground identity + cursor.
          3. ``ElementFinder.overlay_walk(foreground)`` walks the focused window
             and numbers EVERY interactive control 1..K (no clear-winner rule).
          4. Map the walk outcome to the schema outcome (ok / no_targets /
             execution_failed) and emit exactly one ``StartOverlayWalkResponse``
             on the response queue, echoing ``overlay_session_id`` +
             ``paint_generation`` + ``trace_id`` so Logic can drop a superseded
             walk's response by generation.

        ``settle=True`` (wh-overlay-slow-uia-stale-badges.2) changes step 3
        only: instead of one walk, the window is read through
        ``ui.settle_detector.wait_for_settled_window`` until two consecutive
        reads agree, and the settled read is then compared against
        ``compare_snapshot_id`` -- the snapshot Logic pinned before the click.
        When the two comparison lists are equal the response carries THAT id
        and THAT summary, which is how Logic learns to restore the numbers it
        already had; when they differ the fresh snapshot is the answer, exactly
        as for a plain walk. Ids are never reused (``walk-<uuid4 hex>-<n>``
        with a per-run salt and a monotonic counter), so the held id in a
        response can only mean "unchanged".

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter does
        not clobber the walk outcome. It NEVER raises: any unexpected error is
        mapped to a ``status="error"`` / ``outcome="error"`` response so the
        one-response-per-request_id contract holds and the Logic awaiter does
        not fall through to its timeout path. The status/outcome split follows
        the documented r2.10 mapping: a feature failure (no_targets /
        execution_failed) rides transport ``status="ok"``; ``status="error"``
        is reserved for a handler-level crash where the outcome is unreliable.
        """
        from services.wheelhouse.shared.start_overlay_walk import (
            StartOverlayWalkResponse,
        )

        action_name = "start_overlay_walk"

        # wh-overlay-slow-uia-stale-badges.2.2.1: the reply names the window
        # this handler ACTUALLY READ, so Logic can refuse to paint a list over
        # a window it does not describe. Filled in from the ONE
        # ``ForegroundContext`` captured below -- the same context every settle
        # read walks against -- so it is the read's window, not whatever is in
        # front when the reply is parsed. It starts at the same per-field
        # sentinels ``_capture_click_foreground`` degrades to, which is what a
        # short-circuit emitted BEFORE the capture (automation unavailable,
        # overlay disabled) sends; a sentinel identity matches no real
        # foreground, so Logic fails closed on it.
        read_identity: dict = {
            "foreground_window": 0,
            "foreground_pid": 0,
            "foreground_process_name": "",
            "foreground_window_creation_time": 0,
        }

        def _emit(response: "StartOverlayWalkResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "start_overlay_walk: failed to enqueue response "
                    "(trace_id=%s): %s",
                    trace_id, exc,
                )

        def _failed(outcome: str, reason: Optional[str], status: str = "ok",
                    snapshot_id: Optional[str] = None,
                    snapshot_summary=None) -> "StartOverlayWalkResponse":
            return StartOverlayWalkResponse(
                status=status,
                outcome=outcome,
                reason=reason,
                snapshot_id=snapshot_id,
                snapshot_summary=snapshot_summary,
                trace_id=trace_id,
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
                **read_identity,
            )

        try:
            finder = self._get_overlay_walk_finder()
            if finder is None:
                # finder is None either because the COM root could not be built
                # on this host (the _AUTOMATION_UNAVAILABLE sentinel, set by the
                # shared _get_click_element_finder) or because the overlay is
                # genuinely disabled in config (operator opt-out, an overlay-key
                # validation failure, or the whole feature off). Emit the
                # matching tag so the notice is accurate (wh-n29v.74.1, deepseek
                # reviewer_2): clicking IS enabled in config on the
                # COM-unavailable path, so "disabled_by_config" would wrongly
                # point the user at config.toml. Logic also short-circuits
                # before sending; this defends the Input side against a stale or
                # racing request.
                if (
                    getattr(self, "_click_automation_root", None)
                    is _AUTOMATION_UNAVAILABLE
                ):
                    logger.info(
                        "start_overlay_walk: IUIAutomation root unavailable on "
                        "this host; short-circuiting (trace_id=%s)", trace_id,
                    )
                    _emit(_failed("execution_failed", "automation_unavailable"))
                    return
                logger.info(
                    "start_overlay_walk: overlay disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                _emit(_failed("execution_failed", "disabled_by_config"))
                return

            # reviewer_0 finding 38.2: anchor the ONE per-request walk deadline
            # at the dequeue instant the input_proc command reader captured
            # (threaded in as command_dequeue_monotonic), exactly as
            # click_element does (wh-9f3t.73.1). Charging from that earliest
            # reader instant -- not this handler's entry -- folds the ~1s
            # pre-handler reader stall into the budget so the walk gives up
            # before the Logic walk_in_flight timeout rather than after it.
            # The fallback (a direct call with no anchor, e.g. a unit test) uses
            # this handler's entry instant. The bound is the SCREEN READ's own
            # one, ClickConfig.screen_read_walk_deadline_ms -- [click]
            # screen_read_timeout_ms minus the pre-walk margin, 9750 ms at the
            # shipped default -- NOT walk_deadline_ms, which bounds the by-name
            # click walk and is still validated strictly below the click reply
            # limit (wh-overlay-slow-uia-stale-badges.3). Reading it here is
            # what lets a read that answers correctly after 2500 ms be used
            # instead of discarded. It comes from the validated _click_config
            # (set as a side effect of the finder build); None when no bound is
            # configured (defensive -- finder is non-None here). overlay_walk
            # already accepts deadline= and threads it into walk_window
            # unchanged.
            dequeue_monotonic = (
                command_dequeue_monotonic
                if command_dequeue_monotonic is not None
                else time.monotonic()
            )
            walk_deadline_ms = getattr(
                getattr(self, "_click_config", None),
                "screen_read_walk_deadline_ms",
                None,
            )
            walk_deadline: Optional[float] = (
                dequeue_monotonic + (walk_deadline_ms / 1000.0)
                if walk_deadline_ms is not None
                else None
            )

            foreground = _capture_click_foreground()
            # Record the window every read below runs against, so the reply
            # carries it (wh-overlay-slow-uia-stale-badges.2.2.1). This is the
            # ONE capture the settle loop reuses for every read, so it names
            # the window the answer describes even when the foreground moves
            # while the window settles.
            read_identity.update(
                foreground_window=foreground.foreground_window,
                foreground_pid=foreground.foreground_pid,
                foreground_process_name=foreground.foreground_process_name,
                foreground_window_creation_time=(
                    foreground.foreground_window_creation_time
                ),
            )
            logger.info(
                "start_overlay_walk: %s focused window in process=%s "
                "(session=%s gen=%s settle=%s trace_id=%s)",
                "settling" if settle else "walking",
                foreground.foreground_process_name,
                overlay_session_id, paint_generation, settle, trace_id,
            )
            unchanged = False
            held_summary = None
            if settle:
                walk, held_summary = self._settle_overlay_walk(
                    finder,
                    foreground,
                    walk_deadline=walk_deadline,
                    compare_snapshot_id=compare_snapshot_id,
                    trace_id=trace_id,
                )
                unchanged = held_summary is not None
            else:
                walk = finder.overlay_walk(foreground, deadline=walk_deadline)

            if walk.outcome == "execution_failed":
                logger.info(
                    "start_overlay_walk: execution_failed reason=%s "
                    "(trace_id=%s)", walk.reason, trace_id,
                )
                _emit(_failed(
                    "execution_failed",
                    walk.reason or "walk_failed",
                ))
                return

            snapshot_id = (
                walk.snapshot.snapshot_id if walk.snapshot is not None else None
            )
            summary = walk.summary
            if unchanged:
                # Nothing changed since the click. Answer with the HELD
                # snapshot and its OWN summary: a summary names the snapshot
                # it was projected from, and StartOverlayWalkResponse REFUSES
                # a response whose snapshot_summary.snapshot_id differs from
                # its top-level snapshot_id (shared/start_overlay_walk.py:266)
                # because Logic keys the retained summary by the top-level id.
                # The fresh walk's summary names the fresh snapshot, so that
                # pairing would be rejected at the process boundary and the
                # whole response lost. _settle_overlay_walk only reports
                # unchanged when it has the held summary in hand.
                snapshot_id = compare_snapshot_id
                summary = held_summary
            if walk.outcome == "no_targets":
                logger.info(
                    "start_overlay_walk: no_targets (trace_id=%s)", trace_id,
                )
                _emit(StartOverlayWalkResponse(
                    status="ok",
                    outcome="no_targets",
                    reason=None,
                    snapshot_id=snapshot_id,
                    snapshot_summary=summary,
                    trace_id=trace_id,
                    overlay_session_id=overlay_session_id,
                    paint_generation=paint_generation,
                    **read_identity,
                ))
                return

            # outcome == "ok".
            item_count = len(summary.items) if summary is not None else 0
            logger.info(
                "start_overlay_walk: ok with %d items (snapshot=%s "
                "unchanged=%s trace_id=%s)",
                item_count, snapshot_id, unchanged, trace_id,
            )
            _emit(StartOverlayWalkResponse(
                status="ok",
                outcome="ok",
                reason=None,
                snapshot_id=snapshot_id,
                snapshot_summary=summary,
                trace_id=trace_id,
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
                **read_identity,
            ))
        except _TRANSIENT_WALK_ERRORS as exc:
            # Known transient class (rebuilding window, e.g. a theme switch):
            # a feature failure on the normal execution_failed path, logged at
            # WARNING so no error notification pops for a self-healing blip.
            logger.warning(
                "start_overlay_walk: transient UIA/COM walk error, likely a "
                "rebuilding window; mapping to execution_failed "
                "(trace_id=%s): %r",
                trace_id, exc,
            )
            _emit(_failed("execution_failed", "transient_com_error"))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "start_overlay_walk: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            _emit(_failed("error", "unexpected_error", status="error"))

    def _get_overlay_walk_finder(self):
        """Return the ElementFinder for the numbered overlay, or None if off.

        The overlay gates on ``ClickConfig.overlay_enabled_effective`` (NOT just
        ``enabled``): a bad overlay key disables ONLY the overlay while by-name
        click stays operative, and a valid ``overlay_enabled=false`` is an
        operator opt-out. Reuses ``_get_click_element_finder`` to build (and
        memoise) the same finder + validated ``_click_config`` -- the overlay
        and by-name click share one ElementFinder over one ``[click]`` block --
        and then applies the overlay-specific gate on top.

        Returns the cached finder when the overlay is effectively enabled, else
        ``None`` so the handler short-circuits before any walk.
        """
        finder = self._get_click_element_finder()
        # _get_click_element_finder set self._click_config as a side effect
        # (even on its None return path, via the lazy-config guard).
        click_cfg = getattr(self, "_click_config", None)
        if click_cfg is None or not click_cfg.overlay_enabled_effective:
            return None
        # When by-name click is disabled but the overlay somehow validated,
        # _get_click_element_finder returns None; overlay_enabled_effective is
        # False in that case too (the disabled path sets overlay_enabled=False),
        # so the guard above already returned None. finder is non-None here.
        return finder

    def pin_snapshot(
        self,
        overlay_session_id: int = 0,
        snapshot_id: str = "",
        paint_generation: int = 0,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Pin a stored snapshot so LRU eviction skips it (wh-n29v.41).

        Logic owns the active-overlay pin and dispatches ``pin_snapshot`` when
        it dispatches the ``paint_overlay`` that displays a snapshot. The Input
        process drives the multi-snapshot store's :meth:`ElementFinder.pin`
        (skips LRU eviction; the pin is still TTL-bounded). Logic does NOT block
        the paint on this ack, but the handler still emits exactly one
        ``PinSnapshotResponse`` so the Logic-side awaiting Future resolves.

        Stale rejection (the design point, r1c.2 / r1c.1). The store keys
        snapshots by ``snapshot_id`` ONLY and tracks no generation; Logic owns
        the authoritative generation comparison and drops a superseded WALK
        response before it would ever dispatch a pin. As Input-side
        defence-in-depth, this handler keeps a SINGLE bounded watermark
        ``(latest_session_id, latest_accepted_paint_generation)`` (not a
        per-session dict, which would grow one permanent entry per overlay
        session over the process lifetime -- wh-n29v.42.1). ``overlay_session_id``
        is monotonic (allocated when the overlay state machine leaves
        ``closed``) and the state machine is single, so a pin is rejected
        WITHOUT touching the store when EITHER its ``overlay_session_id`` is
        OLDER than the latest seen (a superseded session Logic has torn down)
        OR, within the latest session, its ``paint_generation`` is STRICTLY
        OLDER than the latest accepted one (a racing or duplicated out-of-order
        dispatch). Binding a stale pin could pin the wrong snapshot against a
        session/generation Logic has already advanced. Equal-or-newer
        generations within the latest session and any newer session are
        accepted (an equal generation is the legitimate re-dispatch of the same
        paint; r2.5 / line 366 of the v4 design).

        The watermark advances on ANY accepted (non-stale) dispatch BEFORE the
        store pin, NOT only on a successful pin (wh-n29v.42.2). A failed pin --
        the snapshot was already TTL-evicted -- does not make its generation
        stale, so it must still advance "latest seen"; otherwise a later,
        strictly-older generation for the same session would slip through the
        guard.

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter does
        not clobber the ack. It NEVER raises: any unexpected error is mapped to
        a ``status="error"`` response so the one-response-per-request_id
        contract holds and the Logic awaiter does not fall through to its
        timeout path.
        """
        from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

        action_name = "pin_snapshot"

        def _emit(
            *, status: str, reason: Optional[str], pinned: bool,
        ) -> None:
            # Coerce the echoed identity to schema-safe primitives so the
            # response always parses on the Logic side, even when this handler
            # is rejecting malformed input (wh-n29v.43.1). PinSnapshotResponse
            # .from_dict requires overlay_session_id to be a non-bool int and
            # snapshot_id to be a str; a malformed echo would otherwise raise
            # PinSnapshotResponseSchemaError on Logic instead of resolving the
            # awaiting Future cleanly.
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_snapshot = snapshot_id if isinstance(snapshot_id, str) else ""
            payload = PinSnapshotResponse(
                status=status,
                reason=reason,
                overlay_session_id=safe_session,
                snapshot_id=safe_snapshot,
                pinned=pinned,
            ).to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "pin_snapshot: failed to enqueue response "
                    "(session=%s snapshot=%s): %s",
                    overlay_session_id, snapshot_id, exc,
                )

        try:
            # Validate IPC fields before touching the store or the watermark
            # (wh-n29v.43.1). Logic constructs these, but a malformed message
            # (wrong types from a Logic bug or corruption) must NOT reach the
            # single-pair watermark: a non-int written there makes every later
            # valid pin raise on the comparison (e.g. 5 < "bad") and return
            # status=error until the Input process restarts. overlay_session_id
            # and paint_generation must be non-bool ints (bool is an int
            # subclass; an overlay session / generation is a real count) and
            # snapshot_id must be a str. Reject without store or watermark
            # mutation, emitting exactly one response.
            if (
                not isinstance(overlay_session_id, int)
                or isinstance(overlay_session_id, bool)
                or not isinstance(paint_generation, int)
                or isinstance(paint_generation, bool)
                or not isinstance(snapshot_id, str)
            ):
                logger.error(
                    "pin_snapshot: invalid request fields; rejecting without "
                    "touching store or watermark "
                    "(session=%r snapshot=%r gen=%r)",
                    overlay_session_id, snapshot_id, paint_generation,
                )
                _emit(status="error", reason="invalid_request", pinned=False)
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                # Overlay disabled by config (operator opt-out, an overlay-key
                # validation failure, or the whole feature disabled). Logic
                # gates before sending; defend the Input side too.
                logger.info(
                    "pin_snapshot: overlay disabled by config; "
                    "short-circuiting (session=%s snapshot=%s)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="ok", reason="disabled_by_config", pinned=False)
                return

            # Single-pair stale watermark (wh-n29v.42.1 / wh-n29v.42.2). Track
            # ONE (latest_session_id, latest_accepted_generation) pair, not a
            # per-session dict. overlay_session_id is monotonic and the overlay
            # state machine is single, so two staleness cases are rejected:
            #   * an OLDER overlay_session_id is from a superseded session that
            #     Logic has already torn down; and
            #   * within the latest session, a STRICTLY-OLDER paint_generation
            #     is a racing or duplicated out-of-order dispatch.
            # Equal-or-newer (same session) and any newer session are accepted.
            watermark = getattr(self, "_latest_pin_watermark", None)
            if watermark is not None:
                latest_session, latest_gen = watermark
                if overlay_session_id < latest_session:
                    logger.info(
                        "pin_snapshot: stale session rejected "
                        "(session=%s snapshot=%s < latest_session=%s)",
                        overlay_session_id, snapshot_id, latest_session,
                    )
                    _emit(status="ok", reason="stale_session", pinned=False)
                    return
                if (overlay_session_id == latest_session
                        and paint_generation < latest_gen):
                    logger.info(
                        "pin_snapshot: stale generation rejected "
                        "(session=%s snapshot=%s gen=%s < accepted=%s)",
                        overlay_session_id, snapshot_id, paint_generation,
                        latest_gen,
                    )
                    _emit(status="ok", reason="stale_generation", pinned=False)
                    return

            # Accepted (non-stale): advance the watermark BEFORE the store pin
            # so a FAILED pin (snapshot already TTL-evicted) still advances
            # "latest seen". The snapshot being absent does not make the
            # generation stale -- a later strictly-older generation must still
            # be rejected (wh-n29v.42.2). A newer session resets the generation
            # watermark to this dispatch's generation.
            if (watermark is not None
                    and overlay_session_id == watermark[0]):
                new_gen = max(watermark[1], paint_generation)
            else:
                new_gen = paint_generation
            self._latest_pin_watermark = (overlay_session_id, new_gen)

            pinned = finder.pin(snapshot_id)
            if pinned:
                logger.info(
                    "pin_snapshot: pinned (session=%s snapshot=%s gen=%s)",
                    overlay_session_id, snapshot_id, paint_generation,
                )
                _emit(status="ok", reason=None, pinned=True)
            else:
                logger.info(
                    "pin_snapshot: unknown snapshot (session=%s snapshot=%s)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="ok", reason="unknown_snapshot", pinned=False)
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "pin_snapshot: unexpected error (session=%s snapshot=%s): %s",
                overlay_session_id, snapshot_id, exc, exc_info=True,
            )
            _emit(status="error", reason="unexpected_error", pinned=False)

    def unpin_snapshot(
        self,
        overlay_session_id: int = 0,
        snapshot_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Clear a stored snapshot's pin (wh-n29v.41).

        ``unpin_snapshot`` is clear-by-identity: it carries
        ``(overlay_session_id, snapshot_id)`` only and needs NO generation
        check. Clearing a pin only relaxes LRU immunity, so it is always safe
        to apply regardless of generation -- unlike ``pin_snapshot``, which
        could bind the wrong snapshot against a session Logic has advanced.
        The handler drives the multi-snapshot store's
        :meth:`ElementFinder.unpin` and emits exactly one ``PinSnapshotResponse``
        with ``pinned=False`` so the Logic-side awaiting Future resolves.

        The handler validates its IPC field types up front and is in
        ``_HANDLES_OWN_RESPONSE``. It is never-raise: a malformed message is
        rejected with ``status="error" reason="invalid_request"`` and any
        unexpected error maps to a ``status="error"`` response. Exactly one
        response is emitted per ``request_id`` (the single exception is a dead
        response queue, where the enqueue itself fails -- see the module
        docstring of ``shared/pin_snapshot.py``).
        """
        from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

        action_name = "unpin_snapshot"

        def _emit(*, status: str, reason: Optional[str]) -> None:
            # Coerce the echoed identity to schema-safe primitives so the
            # response always parses on the Logic side, even when this handler
            # is rejecting malformed input (wh-n29v.44.1, mirroring
            # pin_snapshot). PinSnapshotResponse.from_dict requires
            # overlay_session_id to be a non-bool int and snapshot_id to be a
            # str; a malformed echo would otherwise raise
            # PinSnapshotResponseSchemaError on Logic instead of resolving the
            # awaiting Future cleanly.
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_snapshot = snapshot_id if isinstance(snapshot_id, str) else ""
            payload = PinSnapshotResponse(
                status=status,
                reason=reason,
                overlay_session_id=safe_session,
                snapshot_id=safe_snapshot,
                # An unpin's resulting pin state is always "not pinned".
                pinned=False,
            ).to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "unpin_snapshot: failed to enqueue response "
                    "(session=%s snapshot=%s): %s",
                    overlay_session_id, snapshot_id, exc,
                )

        try:
            # Validate IPC fields before touching the store (wh-n29v.44.1).
            # unpin has no watermark to poison (unlike pin_snapshot), but a
            # malformed (overlay_session_id, snapshot_id) echo would make the
            # Logic-side from_dict raise instead of resolving the awaiting
            # Future. Mirror pin_snapshot: reject malformed input without
            # touching the store, emitting exactly one response.
            # overlay_session_id must be a non-bool int (bool is an int
            # subclass; an overlay session is a real count) and snapshot_id a
            # str. unpin carries no paint_generation, so only two fields.
            if (
                not isinstance(overlay_session_id, int)
                or isinstance(overlay_session_id, bool)
                or not isinstance(snapshot_id, str)
            ):
                logger.error(
                    "unpin_snapshot: invalid request fields; rejecting without "
                    "touching store (session=%r snapshot=%r)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="error", reason="invalid_request")
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                logger.info(
                    "unpin_snapshot: overlay disabled by config; "
                    "short-circuiting (session=%s snapshot=%s)",
                    overlay_session_id, snapshot_id,
                )
                _emit(status="ok", reason="disabled_by_config")
                return

            present = finder.unpin(snapshot_id)
            logger.info(
                "unpin_snapshot: %s (session=%s snapshot=%s)",
                "cleared" if present else "unknown",
                overlay_session_id, snapshot_id,
            )
            _emit(
                status="ok",
                reason=None if present else "unknown_snapshot",
            )
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "unpin_snapshot: unexpected error (session=%s snapshot=%s): %s",
                overlay_session_id, snapshot_id, exc, exc_info=True,
            )
            _emit(status="error", reason="unexpected_error")

    def refresh_overlay_snapshot(
        self,
        overlay_session_id: int = 0,
        snapshot_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Slide the Input store's TTL for the still-RETAINED pinned snapshot.

        The Input side of the Logic-side 15-second overlay keepalive
        (wh-overlay-snapshot-keepalive). Logic sends this every keepalive tick
        for the snapshot it still holds pinned; the handler slides that
        snapshot's TTL anchor via
        :meth:`ElementFinder.refresh_snapshot_ttl` so a numbered overlay left on
        screen past the TTL stays clickable. Without it the Logic resolver cache
        and the Input store expire independently and "click N" fails with
        ``snapshot_expired`` on a still-visible overlay.

        The badges are not always on screen when this arrives. Logic also sends
        it while its overlay is PAUSED or POST_CLICK_SETTLING, which clear the
        badges but keep the pin -- for a repaint on resume, or for the
        post-click comparison. See ``_overlay_keepalive_states`` in main.py
        (wh-overlay-slow-uia-stale-badges.13.6).

        Logic does NOT block on the ack, but the handler emits exactly one
        ``PinSnapshotResponse`` (reused as the small Schema-A ack) so the
        Logic-side awaiting Future resolves. The ``pinned`` field echoes whether
        the store found and refreshed the snapshot.

        Unlike ``pin_snapshot`` there is NO stale-generation watermark: a refresh
        carries no generation, and refreshing a superseded snapshot's TTL briefly
        is harmless (it is unpinned and aged out normally). The handler is in
        ``_HANDLES_OWN_RESPONSE`` so the generic emitter does not clobber the
        ack. It NEVER raises: any unexpected error maps to ``status="error"``.
        """
        from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

        action_name = "refresh_overlay_snapshot"

        def _emit(*, status: str, reason: Optional[str], pinned: bool) -> None:
            # Coerce the echoed identity to schema-safe primitives so the
            # response always parses on the Logic side (mirrors pin_snapshot).
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_snapshot = snapshot_id if isinstance(snapshot_id, str) else ""
            payload = PinSnapshotResponse(
                status=status,
                reason=reason,
                overlay_session_id=safe_session,
                snapshot_id=safe_snapshot,
                pinned=pinned,
            ).to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "refresh_overlay_snapshot: failed to enqueue response "
                    "(snapshot=%s): %s",
                    snapshot_id, exc,
                )

        try:
            if not isinstance(snapshot_id, str):
                logger.error(
                    "refresh_overlay_snapshot: invalid snapshot_id; rejecting "
                    "without touching the store (snapshot=%r)", snapshot_id,
                )
                _emit(status="error", reason="invalid_request", pinned=False)
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                logger.info(
                    "refresh_overlay_snapshot: overlay disabled by config; "
                    "short-circuiting (snapshot=%s)", snapshot_id,
                )
                _emit(status="ok", reason="disabled_by_config", pinned=False)
                return

            refreshed = finder.refresh_snapshot_ttl(snapshot_id)
            _emit(
                status="ok",
                reason=None if refreshed else "unknown_snapshot",
                pinned=refreshed,
            )
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "refresh_overlay_snapshot: unexpected error (snapshot=%s): %s",
                snapshot_id, exc, exc_info=True,
            )
            _emit(status="error", reason="unexpected_error", pinned=False)

    def show_numbered_overlay(
        self,
        snapshot_id: str = "",
        item_id_filter: Optional[list[str]] = None,
        overlay_session_id: int = 0,
        paint_generation: int = 0,
        trace_id: str = "",
        request_id: Optional[str] = None,
        command_dequeue_monotonic: Optional[float] = None,
        **kwargs,
    ) -> None:
        """Re-paint an EXISTING walk snapshot for the numbered overlay (wh-n29v.83).

        Unlike the sibling ``start_overlay_walk`` (which walks the focused
        window FROM SCRATCH), ``show_numbered_overlay`` LOOKS UP a snapshot the
        multi-snapshot store already holds and re-paints it. Logic dispatches
        it to re-display a snapshot it retained -- e.g. an auto-open after an
        ambiguous by-name click, restricting the painted set to the ambiguous
        finalists via ``item_id_filter``. The Input process owns the lookup:

          1. Gate on the validated overlay config exactly as
             ``start_overlay_walk`` does, via ``_get_overlay_walk_finder()``:
             short-circuit (no lookup) to ``outcome="execution_failed"`` with
             reason ``automation_unavailable`` when the COM root is the
             ``_AUTOMATION_UNAVAILABLE`` sentinel, or ``disabled_by_config``
             when the overlay is genuinely off. Defence-in-depth -- Logic gates
             too, but the Input side defends against a stale/racing request.
          2. Snapshot the foreground identity + cursor.
          3. ``ElementFinder.get_snapshot(snapshot_id, ...)`` resolves the id
             with the captured foreground identity. A ``None`` return -- stale
             id, TTL-swept, LRU-evicted, or foreground-identity mismatch -- is
             the ``snapshot_expired`` signal.
          4. On a hit, ``ElementFinder._build_summary`` projects the snapshot to
             a display summary, then ``filter_and_renumber_summary`` keeps only
             ``item_id_filter`` items (when supplied) and renumbers the kept set
             1..K in reading order so the badges are contiguous from 1.
          5. An empty post-filter item set is ``no_targets`` (carrying an
             empty-items summary, never a populated one). A non-empty set is
             ``outcome="ok"`` with the rebuilt summary and the snapshot id; the
             summary names the SAME snapshot id (cross-field rule (c)).

        Every response echoes ``overlay_session_id`` + ``paint_generation`` +
        ``trace_id`` verbatim so Logic can drop a superseded paint by
        generation. The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic
        emitter does not clobber the outcome. It NEVER raises: any unexpected
        error is mapped to ``status="error"`` / ``outcome="error"`` so the
        one-response-per-request_id contract holds and the Logic awaiter does
        not fall through to its timeout path. The status/outcome split follows
        the documented r2.10 mapping: a feature failure (snapshot_expired /
        no_targets / execution_failed) rides transport ``status="ok"``;
        ``status="error"`` is reserved for a handler-level crash where the
        outcome is unreliable. The real handler NEVER emits
        ``status="not_implemented"`` (the stub-only parse literal).

        Emits exactly one ShowNumberedOverlayResponse Schema A response on the
        response queue, augmented with ``request_id`` and ``action`` so the
        demuxer in ``app.py`` can resolve the awaiting Future.
        """
        from services.wheelhouse.shared.show_numbered_overlay import (
            ShowNumberedOverlayResponse,
        )
        from ui.element_finder import (
            ElementFinder,
            collapse_near_identical_containers,
        )

        action_name = "show_numbered_overlay"

        def _emit(response: "ShowNumberedOverlayResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "show_numbered_overlay: failed to enqueue response "
                    "(trace_id=%s snapshot=%s): %s",
                    trace_id, snapshot_id, exc,
                )

        def _build(
            outcome: str,
            reason: Optional[str],
            *,
            status: str = "ok",
            snapshot_id_out: Optional[str] = None,
            snapshot_summary=None,
        ) -> "ShowNumberedOverlayResponse":
            # Coerce the echoed scalars to schema-safe primitives so EVERY
            # response this handler builds passes the Logic-side
            # ShowNumberedOverlayResponse.from_dict, even when the inbound Logic
            # message carried malformed echo fields (a Logic bug or corruption).
            # from_dict requires trace_id to be a str and overlay_session_id /
            # paint_generation to be non-bool ints; a response that fails it is
            # log-dropped, which would break the handler-owned one-response
            # contract (the awaiting Future never resolves with a usable
            # result). For a well-formed message these are no-ops. The early
            # invalid_request reject below stops a malformed request before any
            # lookup; this coercion is the belt-and-suspenders guarantee that
            # the error response itself -- and the never-raise error path -- are
            # always serialisable.
            safe_trace = trace_id if isinstance(trace_id, str) else ""
            safe_session = (
                overlay_session_id
                if isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                else 0
            )
            safe_generation = (
                paint_generation
                if isinstance(paint_generation, int)
                and not isinstance(paint_generation, bool)
                else 0
            )
            return ShowNumberedOverlayResponse(
                status=status,
                outcome=outcome,
                reason=reason,
                snapshot_id=snapshot_id_out,
                snapshot_summary=snapshot_summary,
                trace_id=safe_trace,
                overlay_session_id=safe_session,
                paint_generation=safe_generation,
            )

        try:
            # Reject a malformed Logic message before any lookup, mirroring the
            # pin_snapshot / unpin_snapshot IPC-field guard (wh-n29v.43.1).
            # trace_id must be a str, overlay_session_id / paint_generation
            # non-bool ints, snapshot_id a str, and item_id_filter None or a
            # list of str (a non-list filter would also break
            # filter_and_renumber_summary's set() membership build). _build
            # coerces the echoed scalars, so this invalid_request response still
            # passes from_dict; rejecting here means we never run a snapshot
            # lookup against a malformed snapshot_id / filter. status="error"
            # pairs with outcome="error" (cross-field rule (a)); the snapshot
            # fields stay None (rule (d)).
            if not (
                isinstance(trace_id, str)
                and isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                and isinstance(paint_generation, int)
                and not isinstance(paint_generation, bool)
                and isinstance(snapshot_id, str)
                and (
                    item_id_filter is None
                    or (
                        isinstance(item_id_filter, list)
                        and all(isinstance(x, str) for x in item_id_filter)
                    )
                )
            ):
                logger.error(
                    "show_numbered_overlay: invalid request fields; rejecting "
                    "without lookup (trace=%r session=%r gen=%r snapshot=%r "
                    "filter_type=%s)",
                    trace_id, overlay_session_id, paint_generation, snapshot_id,
                    type(item_id_filter).__name__,
                )
                _emit(_build("error", "invalid_request", status="error"))
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                # finder is None either because the COM root could not be built
                # on this host (the _AUTOMATION_UNAVAILABLE sentinel set by the
                # shared _get_click_element_finder) or because the overlay is
                # genuinely disabled in config. Emit the matching reason tag so
                # the notice is accurate (mirrors start_overlay_walk): clicking
                # IS enabled in config on the COM-unavailable path, so
                # "disabled_by_config" would wrongly point the user at
                # config.toml. Logic also short-circuits before sending; this
                # defends the Input side against a stale or racing request. Both
                # are execution_failed (in _NO_SNAPSHOT_OUTCOMES) so the snapshot
                # fields stay None per cross-field rule (d).
                if (
                    getattr(self, "_click_automation_root", None)
                    is _AUTOMATION_UNAVAILABLE
                ):
                    logger.info(
                        "show_numbered_overlay: IUIAutomation root unavailable "
                        "on this host; short-circuiting (trace_id=%s)", trace_id,
                    )
                    _emit(_build("execution_failed", "automation_unavailable"))
                    return
                logger.info(
                    "show_numbered_overlay: overlay disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                _emit(_build("execution_failed", "disabled_by_config"))
                return

            foreground = _capture_click_foreground()
            logger.info(
                "show_numbered_overlay: looking up snapshot=%s in process=%s "
                "(session=%s gen=%s filter=%s trace_id=%s)",
                snapshot_id, foreground.foreground_process_name,
                overlay_session_id, paint_generation,
                None if item_id_filter is None else len(item_id_filter),
                trace_id,
            )

            snapshot = finder.get_snapshot(
                snapshot_id,
                current_foreground_window=foreground.foreground_window,
                current_foreground_pid=foreground.foreground_pid,
                current_foreground_process_name=(
                    foreground.foreground_process_name
                ),
                current_foreground_window_creation_time=(
                    foreground.foreground_window_creation_time
                ),
            )
            if snapshot is None:
                # Stale id, TTL-swept, LRU-evicted, or foreground-identity
                # mismatch -- nothing to paint. snapshot_expired is in
                # _NO_SNAPSHOT_OUTCOMES so snapshot_id/summary MUST be None
                # (cross-field rule (d)).
                logger.info(
                    "show_numbered_overlay: snapshot_expired for snapshot=%s "
                    "(trace_id=%s)", snapshot_id, trace_id,
                )
                _emit(_build("snapshot_expired", "stale_snapshot_id"))
                return

            # Build the display summary, then filter to item_id_filter (when
            # supplied) and renumber the kept items 1..K in reading order so the
            # painted badges are contiguous from 1.
            #
            # wh-overlay-nested-dupes.1.2: this path re-paints a by-name find()
            # snapshot, which is stored UNCOLLAPSED (a spoken name may match a
            # container, so find() keeps both). The Brave wrapper+link pair --
            # identical name, identical rectangle -- is exactly the shape that
            # makes find() ambiguous, so without a collapse the auto-open would
            # paint badge 1 and badge 2 on the same pixels at the very moment
            # the user must read a number. Collapse the DISPLAY set (the
            # filter-selected matches, in the snapshot's pre-order) the same
            # way overlay_walk collapses its walk; the stored snapshot is
            # untouched and every surviving item_id still resolves for
            # click_snapshot_item.
            keep = set(item_id_filter) if item_id_filter is not None else None
            selected = [
                m for m in snapshot.matches
                if keep is None or m.item_id in keep
            ]
            survivors = collapse_near_identical_containers(selected)
            summary = ElementFinder._build_summary(snapshot)
            summary = ElementFinder.filter_and_renumber_summary(
                summary, [m.item_id for m in survivors]
            )

            if not summary.items:
                # Zero interactive controls, or the filter excluded everything.
                # no_targets is the successful-but-empty outcome; it carries the
                # empty-items summary (never a populated one).
                logger.info(
                    "show_numbered_overlay: no_targets for snapshot=%s "
                    "(filter=%s trace_id=%s)",
                    snapshot_id,
                    None if item_id_filter is None else len(item_id_filter),
                    trace_id,
                )
                _emit(_build(
                    "no_targets",
                    None,
                    snapshot_id_out=snapshot.snapshot_id,
                    snapshot_summary=summary,
                ))
                return

            # outcome == "ok": a non-empty painted set. The summary already
            # carries snapshot.snapshot_id (filter_and_renumber preserves it),
            # so summary.snapshot_id == the top-level snapshot_id we echo
            # (cross-field rule (c)).
            logger.info(
                "show_numbered_overlay: ok with %d items "
                "(snapshot=%s trace_id=%s)",
                len(summary.items), snapshot.snapshot_id, trace_id,
            )
            _emit(_build(
                "ok",
                None,
                snapshot_id_out=snapshot.snapshot_id,
                snapshot_summary=summary,
            ))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "show_numbered_overlay: unexpected error "
                "(trace_id=%s snapshot=%s): %s",
                trace_id, snapshot_id, exc, exc_info=True,
            )
            _emit(_build("error", "unexpected_error", status="error"))

    def click_snapshot_item(
        self,
        snapshot_id: str = "",
        item_id: str = "",
        request_id: Optional[str] = None,
        trace_id: str = "",
        gesture: str = "",
        overlay_session_id: Any = None,
        paint_generation: Any = None,
        **kwargs,
    ) -> None:
        """Click a numbered-overlay item by item_id (wh-tab7j / wh-jfavj).

        Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1). When
        the user clicks a numbered overlay badge, Logic resolves the display
        number to an ``item_id`` from its retained ``WalkSnapshotSummary`` and
        forwards this request carrying ``snapshot_id`` + ``item_id``
        (+ ``trace_id`` + ``request_id``, and since wh-click-gesture-param an
        optional ``gesture`` for "right click N" / "double click N"; absent or
        unrecognised means today's Invoke behaviour). The Input process owns
        the click:

          1. Validate the request fields (both ``snapshot_id`` and ``item_id``
             must be non-empty strings) before any lookup.
          2. Get the finder that holds the pinned snapshot store via
             ``_get_overlay_walk_finder`` -- the SAME accessor
             ``show_numbered_overlay`` uses, so it hits the populated store and
             applies the overlay-enabled / automation-unavailable gates.
          3. Capture the current foreground identity.
          4. Look up the pinned snapshot via ``finder.get_snapshot`` with the
             foreground-identity check; ``None`` means stale / TTL-swept /
             LRU-evicted / foreground mismatch -> ``snapshot_expired``.
          5. Find the ``ElementMatch`` in ``snapshot.matches`` whose
             ``item_id`` matches; absence -> ``item_not_found``.
          6. Run ``ClickExecutor.click`` -- the full pre-click verification
             block (foreground identity, IsEnabled, BoundingRectangle, the
             bounds-tolerance check, the popup-still-visible probe) and
             InvokePattern with the DoDefaultAction press fallback. The handler
             does NOT re-implement verification and adds no second COM read.
          7. Emit EXACTLY ONE ``ClickElementResponse`` with the same
             status/outcome pairing and reason tags as ``click_element``:
             ``status="ok"`` only for ``outcome="ok"``, ``status="error"`` for
             ``execution_failed``.

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter does
        not clobber the executor's outcome. It NEVER raises: any unexpected
        error is mapped to an ``execution_failed`` response so the
        one-response-per-request_id contract holds and the Logic awaiter does
        not fall through to its timeout path. ``snapshot_summary`` is always
        ``None`` here -- this handler clicks a pinned snapshot, it does not
        re-walk or repaint.

        wh-overlay-slow-uia-stale-badges.8 part 4: the request MAY carry
        ``overlay_session_id`` + ``paint_generation``, the pair of the visible
        list the spoken number was resolved against. Between the field
        validation and the snapshot lookup the handler refuses the click
        (``stale_overlay_generation``) when that pair is not STRICTLY newer
        than ``_last_executed_click_pair`` -- an earlier click already
        executed against that list and changed the screen, so the number now
        names the wrong control. Only an ok click records the watermark (a
        refusal leaves the screen unchanged, so a retry must work). An absent
        pair (pre-slice payload shape) or a malformed pair skips the check --
        the primary guard is Logic's in-flight refusal; this one is
        defence-in-depth, so it degrades OPEN like the pin watermark.
        """
        from services.wheelhouse.shared.click_element import (
            ClickElementResponse,
        )
        from ui.element_types import ElementQuery

        action_name = "click_snapshot_item"

        # Coerce trace_id to a str so EVERY response this handler builds passes
        # the Logic-side ClickElementResponse.from_dict (trace_id must be a str)
        # even when the inbound Logic message carried a malformed echo field.
        safe_trace = trace_id if isinstance(trace_id, str) else ""

        def _emit(response: "ClickElementResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                # Last-resort: log and drop. Without a response the Logic-side
                # Future times out, which Logic already handles gracefully.
                logger.error(
                    "click_snapshot_item: failed to enqueue response "
                    "(trace_id=%s snapshot=%s item=%s): %s",
                    safe_trace, snapshot_id, item_id, exc,
                )

        def _failed(reason: str, matched_name: Optional[str] = None,
                    snapshot_id_out: Optional[str] = None) -> "ClickElementResponse":
            return ClickElementResponse(
                status="error",
                outcome="execution_failed",
                reason=reason,
                matched_names=(matched_name,) if matched_name else (),
                snapshot_id=snapshot_id_out,
                snapshot_summary=None,
                matched_name=matched_name,
                trace_id=safe_trace,
            )

        try:
            # Reject a malformed Logic message before any lookup, mirroring the
            # show_numbered_overlay IPC-field guard. Both ids must be non-empty
            # strings; the snapshot_id echoed on the invalid_request response
            # stays None so we never echo a non-str id.
            if not (
                isinstance(snapshot_id, str) and snapshot_id
                and isinstance(item_id, str) and item_id
            ):
                logger.error(
                    "click_snapshot_item: invalid request fields; rejecting "
                    "without lookup (snapshot_type=%s item_type=%s "
                    "trace_id=%s)",
                    type(snapshot_id).__name__, type(item_id).__name__,
                    safe_trace,
                )
                _emit(_failed("invalid_request"))
                return

            # wh-overlay-slow-uia-stale-badges.8 part 4: one executed click
            # per (overlay_session_id, paint_generation). Parse the optional
            # pair; a malformed pair degrades OPEN (log, skip the check, and
            # do NOT record it) because the primary guard lives on the Logic
            # side and a type glitch must not refuse a click the user
            # legitimately asked for. bool is excluded explicitly -- it is an
            # int subclass, not a session id or generation.
            resolved_pair: Optional[tuple[int, int]] = None
            if (
                isinstance(overlay_session_id, int)
                and not isinstance(overlay_session_id, bool)
                and isinstance(paint_generation, int)
                and not isinstance(paint_generation, bool)
            ):
                resolved_pair = (overlay_session_id, paint_generation)
            elif overlay_session_id is not None or paint_generation is not None:
                logger.warning(
                    "click_snapshot_item: malformed overlay pair "
                    "(session=%r generation=%r); skipping the stale check "
                    "(trace_id=%s)",
                    overlay_session_id, paint_generation, safe_trace,
                )
            last = getattr(self, "_last_executed_click_pair", None)
            if resolved_pair is not None and last is not None and (
                resolved_pair[0] < last[0]
                or (
                    resolved_pair[0] == last[0]
                    and resolved_pair[1] <= last[1]
                )
            ):
                # The list this number came from was consumed by an earlier
                # executed click (same pair) or belongs to an even older
                # paint. Refuse WITHOUT touching the snapshot or executor:
                # the screen has already changed under the badge.
                logger.info(
                    "click_snapshot_item: stale_overlay_generation -- "
                    "resolved pair %s is not newer than the last executed "
                    "click's pair %s (snapshot=%s item=%s trace_id=%s)",
                    resolved_pair, last, snapshot_id, item_id, safe_trace,
                )
                _emit(_failed(
                    "stale_overlay_generation", snapshot_id_out=snapshot_id,
                ))
                return

            finder = self._get_overlay_walk_finder()
            if finder is None:
                # finder is None either because the COM root could not be built
                # on this host (the _AUTOMATION_UNAVAILABLE sentinel set by the
                # shared _get_click_element_finder) or because the feature is
                # genuinely disabled in config. Emit the matching reason tag so
                # the notice is accurate (mirrors click_element /
                # show_numbered_overlay).
                if (
                    getattr(self, "_click_automation_root", None)
                    is _AUTOMATION_UNAVAILABLE
                ):
                    logger.info(
                        "click_snapshot_item: IUIAutomation root unavailable "
                        "on this host; short-circuiting (trace_id=%s)",
                        safe_trace,
                    )
                    _emit(_failed("automation_unavailable"))
                    return
                logger.info(
                    "click_snapshot_item: feature disabled by config; "
                    "short-circuiting (trace_id=%s)", safe_trace,
                )
                _emit(_failed("disabled_by_config"))
                return

            foreground = _capture_click_foreground()
            # Name the exact miss cause BEFORE get_snapshot is called
            # (wh-overlay-snapshot-keepalive Fix 3). The bare snapshot_expired
            # conflated a TTL expiry (trigger A), a never-stored/evicted id, and
            # a foreground change (trigger B), so a live report could not tell
            # the two triggers apart. describe_snapshot_miss is the non-mutating
            # query that names which one fired -- but get_snapshot MUTATES on a
            # miss (it sweeps a TTL-expired entry and drops a foreground-mismatch
            # entry before returning None), so describe MUST run first or it
            # always sees an already-gone entry and reports not_found for the two
            # real triggers (wh-overlay-snapshot-keepalive.1.1). describe returns
            # None on the hit path, so this is a cheap non-mutating lookup on
            # every numbered-badge click. Best-effort: never let cause-naming
            # break the click path. The emitted Schema-A reason tag stays
            # snapshot_expired (routing/notice behaviour is unchanged); only the
            # log gains the cause.
            miss_cause = None
            describe = getattr(finder, "describe_snapshot_miss", None)
            if callable(describe):
                try:
                    miss_cause = describe(
                        snapshot_id,
                        current_foreground_window=(
                            foreground.foreground_window
                        ),
                        current_foreground_pid=foreground.foreground_pid,
                        current_foreground_process_name=(
                            foreground.foreground_process_name
                        ),
                        current_foreground_window_creation_time=(
                            foreground.foreground_window_creation_time
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 -- log only
                    logger.debug(
                        "click_snapshot_item: describe_snapshot_miss failed "
                        "for snapshot=%s: %s", snapshot_id, exc,
                    )
            snapshot = finder.get_snapshot(
                snapshot_id,
                current_foreground_window=foreground.foreground_window,
                current_foreground_pid=foreground.foreground_pid,
                current_foreground_process_name=(
                    foreground.foreground_process_name
                ),
                current_foreground_window_creation_time=(
                    foreground.foreground_window_creation_time
                ),
            )
            if snapshot is None:
                # Stale id, TTL-swept, LRU-evicted, or foreground-identity
                # mismatch -- nothing to click. miss_cause (computed above,
                # before get_snapshot dropped the entry) names which.
                logger.info(
                    "click_snapshot_item: snapshot_expired (cause=%s) for "
                    "snapshot=%s (trace_id=%s)",
                    miss_cause or "unknown", snapshot_id, safe_trace,
                )
                _emit(_failed("snapshot_expired", snapshot_id_out=snapshot_id))
                return

            # Find the requested item by its stable item_id in the pinned
            # snapshot's matches (a simple linear scan -- the snapshot holds at
            # most a few dozen interactive controls).
            match = next(
                (m for m in snapshot.matches if m.item_id == item_id), None
            )
            if match is None:
                logger.info(
                    "click_snapshot_item: item_not_found item=%s in "
                    "snapshot=%s (trace_id=%s)",
                    item_id, snapshot_id, safe_trace,
                )
                _emit(_failed("item_not_found", snapshot_id_out=snapshot_id))
                return

            from ui.click_executor import SnapshotForeground

            # The overlay-click path has no spoken query; build a minimal
            # ElementQuery from the match. It is consumed ONLY by the executor's
            # coordinate-eligibility check (_coord_eligible) and, since
            # wh-click-gesture-param, by the gesture branch: "right click 5" /
            # "double click 5" arrive as a gesture field on the request, and
            # this is where that value becomes something the executor reads.
            # An absent or unrecognised value degrades to the default Invoke
            # gesture rather than guessing a physical click.
            query = ElementQuery(
                name=match.name,
                role=match.role,
                ordinal=None,
                spatial=None,
                raw_utterance="",
                gesture=_parse_gesture(gesture),
            )
            snap_fg = SnapshotForeground(
                window=foreground.foreground_window,
                pid=foreground.foreground_pid,
                process_name=foreground.foreground_process_name,
                window_creation_time=foreground.foreground_window_creation_time,
            )
            executor = self._get_click_executor()
            # wh-review-pattern-fixes.8: a badge click can move focus, the
            # caret, or the selection; invalidate before the dispatch (see
            # the hotkey_action block comment for the full rationale).
            self.buffer_manager.invalidate()
            # badge_pick=True: a numbered-badge pick prefers the guarded
            # coordinate click over DoDefaultAction when InvokePattern is
            # structurally unavailable (wh-electron-dda-noop).
            click_result = executor.click(match, snap_fg, query, badge_pick=True)
            if click_result.outcome == "ok":
                if resolved_pair is not None:
                    # wh-overlay-slow-uia-stale-badges.8 part 4: only an
                    # EXECUTED click consumes its pair -- a refusal leaves
                    # the screen unchanged, so a retry from the same list
                    # must stay allowed.
                    self._last_executed_click_pair = resolved_pair
                logger.info(
                    "click_snapshot_item: clicked %r via %s "
                    "(item=%s snapshot=%s trace_id=%s)",
                    click_result.matched_name, click_result.clicked_via,
                    item_id, snapshot_id, safe_trace,
                )
                _emit(ClickElementResponse(
                    status="ok",
                    outcome="ok",
                    reason=None,
                    matched_names=(
                        (click_result.matched_name,)
                        if click_result.matched_name else ()
                    ),
                    snapshot_id=snapshot_id,
                    snapshot_summary=None,
                    matched_name=click_result.matched_name,
                    trace_id=safe_trace,
                ))
                return

            logger.info(
                "click_snapshot_item: execution_failed reason=%s matched=%r "
                "(item=%s snapshot=%s trace_id=%s)",
                click_result.reason, click_result.matched_name,
                item_id, snapshot_id, safe_trace,
            )
            _emit(_failed(
                click_result.reason or "invoke_com_error",
                matched_name=click_result.matched_name,
                snapshot_id_out=snapshot_id,
            ))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "click_snapshot_item: unexpected error "
                "(trace_id=%s snapshot=%s item=%s): %s",
                safe_trace, snapshot_id, item_id, exc, exc_info=True,
            )
            _emit(_failed("invoke_com_error"))

    def _get_click_element_finder(self):
        """Lazily build (and memoise) the ElementFinder for voice clicking.

        wh-tab7j wires the deferred ClickConfig-to-ElementFinder
        configuration that wh-1yqgn left as constructor defaults. The
        Input process receives the full raw config dict (it cannot be
        handed a ConfigService across the process boundary), so we run the
        never-raising ``ClickConfig.from_raw`` on the ``[click]`` block and
        feed the validated thresholds into the coordinator. The DPI and
        monitor resolvers are real Win32-backed callables with safe
        fallbacks so a headless / degraded host never crashes the click
        path; tests construct UIActionHandler with a fake config and drive
        ``click_element`` against an injected finder via
        ``self._click_element_finder``.

        Returns the cached finder, or ``None`` when voice clicking is
        disabled by config (so the handler can short-circuit before any
        walk).
        """
        # A test (or a prior call) may have injected/built one already.
        existing = getattr(self, "_click_element_finder", None)
        if existing is not None:
            # The ambiguous branch reads self._click_config.notice_max_names.
            # self._click_config is normally set as a side effect of building
            # the finder below, but the injected-finder test seam (and any
            # caller that sets _click_element_finder directly) can reach this
            # early return with _click_config unset. Without this guard the
            # ambiguous branch would raise AttributeError, which the handler's
            # outer except swallows into a misleading invoke_com_error outcome
            # (wh-9f3t.54.1). Mirror _get_click_executor's lazy-config guard.
            if getattr(self, "_click_config", None) is None:
                from ui.click_config import ClickConfig

                self._click_config = ClickConfig.from_raw(
                    self.config.get("click", {})
                )
            return existing

        from ui import uia_walker
        from ui.click_config import ClickConfig
        from ui.element_finder import ElementFinder

        click_cfg = ClickConfig.from_raw(self.config.get("click", {}))
        self._click_config = click_cfg
        if not click_cfg.enabled:
            # Feature globally off (operator opt-out or a validation
            # failure). The by-name handler short-circuits before walking. We
            # do NOT build the COM root in this branch: a disabled feature
            # never walks, so creating an IUIAutomation root here would be
            # wasted COM state.
            self._click_element_finder = None
            return None

        automation_root = getattr(self, "_click_automation_root", None)

        # A prior call already tried (and failed) to build the COM root on this
        # degraded host (wh-n29v.72.2). Short-circuit to None -- the same
        # behaviour as the disabled-config branch -- WITHOUT re-calling the
        # failing create_automation(). This is the session-level give-up that
        # stops the per-utterance CoCreateInstance retry storm.
        if automation_root is _AUTOMATION_UNAVAILABLE:
            self._click_element_finder = None
            return None

        # The single IUIAutomation root for the whole click feature
        # (wh-n29v.71). COM-threading constraint: an IUIAutomation object must
        # be used on the apartment/thread that created it. This method is
        # called lazily from click_element on the single Input command-reader
        # thread, so creating the root HERE (lazily, the first time a click is
        # served) and memoising it on the handler means the SAME root is both
        # created and used on that one thread across every click. The root is
        # deliberately NOT created in __init__ (which may run on a different
        # thread) nor per-click. The finder threads this root into the PRIMARY
        # walk_window and into the owned-popup walk_owned_popups; the popup
        # walk's control_type_fn (the dead UIA-Menu lookup before this slice)
        # is derived over this SAME root by walk_owned_popups' own
        # _make_default_control_type_of default, so a popup whose ClassName is
        # not the classic #32768 still resolves by UIA control type.
        if automation_root is None:
            try:
                automation_root = uia_walker.create_automation()
            except Exception:  # noqa: BLE001 -- degraded UIA host: give up once
                # create_automation() RAISES (it never returns None) on a host
                # whose UIAutomationCore is broken/locked-down/headless. Memoise
                # the FAILURE so every later click takes the short-circuit above
                # instead of re-attempting CoCreateInstance on the
                # command-reader loop. Log once at error (this branch runs once
                # per session by construction). The disabled-config notice
                # ("Voice clicking is disabled") is reused: the user-visible
                # effect is identical (no walk), and the log line carries the
                # real cause for the operator.
                logger.error(
                    "click_element: IUIAutomation root unavailable on this "
                    "host; voice clicking disabled for this session",
                    exc_info=True,
                )
                self._click_automation_root = _AUTOMATION_UNAVAILABLE
                self._click_element_finder = None
                return None
            # Defence in depth (wh-n29v.73.3): create_automation() is contracted
            # to RAISE on failure and never return None -- its CreateObject
            # raises, and the non-None assert is a static-analysis guarantee that
            # `python -O` STRIPS. If that contract is ever violated and a None
            # reaches here, treat it IDENTICALLY to the raise path above: memoise
            # the session-level give-up. Without this, a None return would fall
            # through, store None as the root, and build ElementFinder(
            # automation=None) -- a partial-wiring state (NOT the
            # _AUTOMATION_UNAVAILABLE give-up) that hands a None root to
            # walk_owned_popups, where UIA-Menu control-type detection and popup
            # subtree walking fail or skip silently and the failure is never
            # memoised.
            if automation_root is None:
                logger.error(
                    "click_element: create_automation() returned None on this "
                    "host; voice clicking disabled for this session"
                )
                self._click_automation_root = _AUTOMATION_UNAVAILABLE
                self._click_element_finder = None
                return None
            self._click_automation_root = automation_root

        finder = ElementFinder(
            automation=automation_root,
            snapshot_ttl_seconds=click_cfg.snapshot_ttl_seconds,
            min_confidence=click_cfg.min_confidence,
            clear_winner_margin=click_cfg.clear_winner_margin,
            tiebreaker_influence_logical_px=float(
                click_cfg.tiebreaker_influence_logical_px
            ),
            tiebreaker_min_separation_logical_px=float(
                click_cfg.tiebreaker_min_separation_logical_px
            ),
            min_substring_query_length=click_cfg.min_substring_query_length,
            min_substring_overlap_ratio=click_cfg.min_substring_overlap_ratio,
            browser_processes=list(click_cfg.browser_processes),
            browser_processes_extend=list(click_cfg.browser_processes_extend),
            dpi_resolver=_win32_dpi_resolver,
            monitor_resolver=_win32_monitor_resolver,
            # The owned-popup walk is now ON in production (wh-n29v.71). We pass
            # a real IUIAutomation root above and the ElementFinder default
            # popup_walk_fn (uia_walker.walk_owned_popups) is left in place, so
            # owned #32768 / UIA-Menu popups are walked and a popup item can win
            # the by-name click. The matching production ClickExecutor
            # (_get_click_executor) now also receives the real IsWindowVisible /
            # GetWindow(GW_OWNER) probe seams so a popup-owned winner is no
            # longer refused with execution_failed:popup_closed. All three
            # injections (root here, real popup walk default, executor probe
            # seams) flip the feature ON together.
            enable_offmonitor_fallback=click_cfg.enable_offmonitor_fallback,
            # Bound the synchronous UIA walk so it gives up no later than the
            # Logic-side click awaiter ([click] response_timeout_ms), keeping the
            # single command-reader loop from starving dictation/hotkeys on a
            # deep subtree (wh-9f3t.54.2). The validator guarantees this is
            # <= response_timeout_ms.
            walk_deadline_ms=click_cfg.walk_deadline_ms,
            snapshot_store_capacity=click_cfg.snapshot_store_capacity,
        )
        self._click_element_finder = finder
        return finder

    def _get_click_executor(self):
        """Lazily build (and memoise) the ClickExecutor for voice clicking."""
        existing = getattr(self, "_click_executor", None)
        if existing is not None:
            return existing

        from ui import uia_walker
        from ui.click_config import ClickConfig
        from ui.click_executor import ClickExecutor

        click_cfg = getattr(self, "_click_config", None)
        if click_cfg is None:
            click_cfg = ClickConfig.from_raw(self.config.get("click", {}))
            self._click_config = click_cfg

        executor = ClickExecutor(
            coordinate_click_fn=_win32_coordinate_click,
            # Gesture coordinate click (wh-click-gesture-param): a right click
            # or double click takes the guarded coordinate path with a
            # different button or count. Without this injection the executor's
            # raising placeholder refuses every non-default gesture.
            gesture_click_fn=_win32_gesture_click,
            foreground_probe=_win32_foreground_probe,
            on_screen_fn=_win32_on_screen,
            enable_coordinate_click_on_com_error=(
                click_cfg.enable_coordinate_click_on_com_error
            ),
            # Phase 1.5 pre-click bounds-tolerance (design r1c.6). Thread the
            # already-validated tolerance from ClickConfig so a control whose
            # freshly-read bounds moved more than this many physical pixels from
            # its cached walk-time bounds is refused (bounds_stale) instead of
            # clicked where the numbered badge no longer points.
            overlay_bounds_tolerance_physical_px=(
                click_cfg.overlay_bounds_tolerance_physical_px
            ),
            # Pre-click verification budget (wh-overlay-slow-uia-stale-badges.6).
            # Thread the already-validated budget from ClickConfig so a slow
            # application's COM reads refuse (verification_timeout) instead of
            # blocking past the Logic awaiter and mislabeling the refusal as a
            # transport timeout.
            verification_budget_ms=click_cfg.verification_budget_ms,
            # Real Win32 popup-closed probe seams (wh-n29v.71). Without these
            # ClickExecutor._popup_still_open fails closed (returns False when
            # either probe is None) and EVERY popup-owned winner is refused
            # execution_failed:popup_closed. _default_is_window_visible wraps
            # IsWindowVisible; _default_owner_of wraps GetWindow(GW_OWNER).
            # These are pure Win32 (no COM apartment affinity), so they are the
            # module-level callables, not bound to the IUIAutomation root.
            popup_visible_fn=uia_walker._default_is_window_visible,
            popup_owner_fn=uia_walker._default_owner_of,
            # Shell-window class re-read (codex finding
            # wh-overlay-taskbar-numbers.5.3): the shell probe additionally
            # requires the walked HWND to STILL name a taskbar shell class at
            # click time, so a handle recycled to an unrelated window after an
            # explorer restart is refused (taskbar_closed) instead of invoked
            # into. _default_class_name_of wraps GetClassName -- pure Win32,
            # same module-level-callable rationale as the two seams above.
            shell_class_fn=uia_walker._default_class_name_of,
            # Click-point hit-test (wh-explorer-navpane-click.1.1): the
            # coordinate fallback refuses before sending when the root window
            # under the click point is not the winner's own top-level window.
            window_at_point_fn=_win32_root_window_at_point,
            # UIA point-hits-winner check (wh-explorer-navpane-click.1.4):
            # the second obstruction layer, for SAME-ROOT occluders. Resolves
            # the IUIAutomation root at CALL time, not construction time: the
            # root is built lazily by _get_click_element_finder, and every
            # winner reaching the executor came from a walk, so the root
            # exists by then. A missing/failed root raises and the executor
            # refuses (click_point_obstructed) rather than clicking blind.
            point_hits_winner_fn=self._point_hits_winner_via_automation_root,
        )
        self._click_executor = executor
        return executor

    def _point_hits_winner_via_automation_root(self, winner, x: int, y: int) -> bool:
        """Run the UIA point check against the memoised automation root.

        Bound seam for ``ClickExecutor.point_hits_winner_fn``
        (wh-explorer-navpane-click.1.4). Raises when the IUIAutomation root
        is absent or the permanently-failed sentinel -- the executor maps the
        raise to a fail-closed ``click_point_obstructed`` refusal. In
        practice unreachable in those states: a winner only exists after a
        successful walk, which requires a working root.
        """
        automation_root = getattr(self, "_click_automation_root", None)
        if automation_root is None or automation_root is _AUTOMATION_UNAVAILABLE:
            raise RuntimeError(
                "no usable IUIAutomation root for the point-hits-winner check"
            )
        return _uia_point_hits_winner(automation_root, winner, x, y)

    def click_element(
        self,
        query: object = None,
        trace_id: str = "",
        request_id: Optional[str] = None,
        command_dequeue_monotonic: Optional[float] = None,
        **kwargs,
    ) -> None:
        """Resolve a voice 'click <target>' and emit one ClickElementResponse.

        wh-tab7j (wh-l4h.1 Phase 1). Logic parses the spoken target into an
        ``ElementQuery`` (wh-vjwdl), generates the trace_id, and forwards
        this request. The Input process owns the walk + click:

          1. Snapshot the foreground identity + cursor.
          2. ``ElementFinder.find(query, foreground)`` walks the focused
             window, scores eligible matches, and runs the clear-winner
             rule.
          3. On an ``ok`` outcome, ``ClickExecutor.click`` runs the full
             pre-click verification block and fires InvokePattern.
          4. Exactly one ``ClickElementResponse`` is emitted via the
             response queue, carrying the outcome, matched names, the
             ``snapshot_id`` + plain-data ``WalkSnapshotSummary`` for the
             Phase 1.5 numbered overlay, and the ``trace_id`` so the Logic
             awaiter and the Input log lines share one correlation id.

        The handler is in ``_HANDLES_OWN_RESPONSE`` so the generic emitter
        does not clobber the executor's outcome. It NEVER raises: any
        unexpected error is mapped to an ``execution_failed`` response so
        the one-response-per-request_id contract holds and the Logic
        awaiter does not fall through to its timeout path.
        """
        from services.wheelhouse.shared.click_element import (
            ClickElementResponse,
        )
        from ui.element_types import ElementQuery

        action_name = "click_element"

        # The ONE per-request walk-budget anchor. The input_proc command-reader
        # loop captures the monotonic instant immediately after it deserializes
        # this message (BEFORE the INPUT_RECEIVED log + dispatch lookup, which
        # the loop documents as a prior ~1.0s stall site) and threads it here as
        # command_dequeue_monotonic (wh-9f3t.73.1). Charging the walk budget from
        # that earliest reader instant -- not from this handler's entry -- folds
        # the pre-handler reader time into the budget so the walk gives up before
        # the Logic awaiter (whose clock started at send_request) times out. The
        # fallback (a direct handler call with no anchor, e.g. a unit test) uses
        # this handler's entry instant. The resolved absolute deadline is passed
        # into ElementFinder.find, which threads it unchanged into every
        # walk_window call (focused + each fall-back) so the total block is
        # bounded by one deadline (FINDING 1), not (1+N) per-window budgets.
        dequeue_monotonic = (
            command_dequeue_monotonic
            if command_dequeue_monotonic is not None
            else time.monotonic()
        )

        def _emit(response: "ClickElementResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:
                logger.error(
                    "click_element: failed to enqueue response "
                    "(trace_id=%s): %s",
                    trace_id, exc,
                )

        def _failed(reason: str, matched_name: Optional[str] = None,
                    snapshot_id: Optional[str] = None,
                    snapshot_summary=None) -> "ClickElementResponse":
            return ClickElementResponse(
                status="error",
                outcome="execution_failed",
                reason=reason,
                matched_names=(matched_name,) if matched_name else (),
                snapshot_id=snapshot_id,
                snapshot_summary=snapshot_summary,
                matched_name=matched_name,
                trace_id=trace_id,
            )

        try:
            if not isinstance(query, ElementQuery):
                logger.warning(
                    "click_element: dropping request with non-ElementQuery "
                    "query=%r (trace_id=%s)", type(query).__name__, trace_id,
                )
                _emit(_failed("malformed_query"))
                return

            finder = self._get_click_element_finder()
            if finder is None:
                # finder is None for one of two distinct reasons; emit the
                # matching tag so the user notice is accurate (wh-n29v.74.1,
                # deepseek reviewer_2):
                #  * the IUIAutomation root could not be built on this host
                #    (the _AUTOMATION_UNAVAILABLE sentinel was set in
                #    _get_click_element_finder). Clicking IS enabled in config,
                #    so a "disabled_by_config" notice would wrongly send the
                #    user to config.toml [click]. Emit "automation_unavailable".
                #  * the feature is genuinely off in config (enabled=false or a
                #    [click] validation failure). That path never sets the
                #    sentinel, so it keeps "disabled_by_config".
                # Logic also short-circuits before sending; this defends the
                # Input side against a stale or racing request.
                if (
                    getattr(self, "_click_automation_root", None)
                    is _AUTOMATION_UNAVAILABLE
                ):
                    logger.info(
                        "click_element: IUIAutomation root unavailable on this "
                        "host; short-circuiting (trace_id=%s)", trace_id,
                    )
                    _emit(_failed("automation_unavailable"))
                    return
                logger.info(
                    "click_element: feature disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                _emit(_failed("disabled_by_config"))
                return

            # Resolve the per-request absolute deadline from the validated
            # walk_deadline_ms (the validator guarantees it is strictly <
            # response_timeout_ms, so the walk gives up before the Logic
            # awaiter). Anchored at dequeue above; None when the feature has no
            # walk bound configured (defensive -- finder is non-None here).
            walk_deadline_ms = getattr(
                getattr(self, "_click_config", None), "walk_deadline_ms", None
            )
            walk_deadline: Optional[float] = (
                dequeue_monotonic + (walk_deadline_ms / 1000.0)
                if walk_deadline_ms is not None
                else None
            )

            foreground = _capture_click_foreground()
            logger.info(
                "click_element: walking for name=%r role=%r in process=%s "
                "(trace_id=%s)",
                query.name, query.role,
                foreground.foreground_process_name, trace_id,
            )
            find_result = finder.find(query, foreground, deadline=walk_deadline)
            outcome = find_result.outcome
            summary = find_result.summary
            snapshot_id = find_result.snapshot.snapshot_id

            if outcome.outcome == "not_found":
                logger.info(
                    "click_element: not_found name=%r (trace_id=%s)",
                    query.name, trace_id,
                )
                _emit(ClickElementResponse(
                    status="ok",
                    outcome="not_found",
                    reason=None,
                    matched_names=(),
                    snapshot_id=snapshot_id,
                    snapshot_summary=summary,
                    matched_name=None,
                    trace_id=trace_id,
                ))
                return

            if outcome.outcome == "ambiguous":
                names = tuple(
                    m.name for m in outcome.candidates if m.name
                )[: self._click_config.notice_max_names]
                # wh-overlay-ambiguous-autoopen (found by deepseek): Logic's
                # auto-open gate (wh-n29v.111) requires ambiguous_item_ids;
                # without it the numbered overlay never auto-opened on an
                # ambiguous by-name click. UNCAPPED on purpose --
                # notice_max_names caps only the notice wording, while the
                # auto-open must badge every finalist.
                finalist_ids = tuple(m.item_id for m in outcome.candidates)
                logger.info(
                    "click_element: ambiguous names=%r finalists=%d "
                    "(trace_id=%s)", names, len(finalist_ids), trace_id,
                )
                _emit(ClickElementResponse(
                    status="ok",
                    outcome="ambiguous",
                    reason=None,
                    matched_names=names,
                    snapshot_id=snapshot_id,
                    snapshot_summary=summary,
                    matched_name=None,
                    trace_id=trace_id,
                    ambiguous_item_ids=finalist_ids,
                ))
                return

            if outcome.outcome == "execution_failed":
                # decide() surfaces a walk-time disabled winner here before
                # the executor runs (distinct from a Logic disabled_by_config
                # short-circuit and from a click-time IsEnabled failure).
                matched = outcome.winner.name if outcome.winner else None
                logger.info(
                    "click_element: walk-time execution_failed reason=%s "
                    "matched=%r (trace_id=%s)",
                    outcome.reason, matched, trace_id,
                )
                _emit(_failed(
                    outcome.reason or "disabled",
                    matched_name=matched,
                    snapshot_id=snapshot_id,
                    snapshot_summary=summary,
                ))
                return

            # outcome == "ok": run the executor against the live winner.
            winner = outcome.winner
            if winner is None:
                # Defensive: decide() never returns ok with a None winner.
                _emit(_failed("invoke_com_error", snapshot_id=snapshot_id,
                              snapshot_summary=summary))
                return

            from ui.click_executor import SnapshotForeground

            snap_fg = SnapshotForeground(
                window=foreground.foreground_window,
                pid=foreground.foreground_pid,
                process_name=foreground.foreground_process_name,
                window_creation_time=foreground.foreground_window_creation_time,
            )
            executor = self._get_click_executor()
            # wh-review-pattern-fixes.8: a by-name click can move focus, the
            # caret, or the selection; invalidate before the dispatch (see
            # the hotkey_action block comment for the full rationale).
            self.buffer_manager.invalidate()
            click_result = executor.click(winner, snap_fg, query)
            if click_result.outcome == "ok":
                logger.info(
                    "click_element: clicked %r via %s (trace_id=%s)",
                    click_result.matched_name, click_result.clicked_via,
                    trace_id,
                )
                _emit(ClickElementResponse(
                    status="ok",
                    outcome="ok",
                    reason=None,
                    matched_names=(
                        (click_result.matched_name,)
                        if click_result.matched_name else ()
                    ),
                    snapshot_id=snapshot_id,
                    snapshot_summary=summary,
                    matched_name=click_result.matched_name,
                    trace_id=trace_id,
                ))
                return

            logger.info(
                "click_element: click-time execution_failed reason=%s "
                "matched=%r (trace_id=%s)",
                click_result.reason, click_result.matched_name, trace_id,
            )
            _emit(_failed(
                click_result.reason or "invoke_com_error",
                matched_name=click_result.matched_name,
                snapshot_id=snapshot_id,
                snapshot_summary=summary,
            ))
        except _TRANSIENT_WALK_ERRORS as exc:
            # Known transient class (rebuilding window, e.g. a theme switch):
            # same response as the generic path, but WARNING-level so no error
            # notification pops for a blip that resolves on the next attempt.
            logger.warning(
                "click_element: transient UIA/COM error, likely a rebuilding "
                "window; mapping to execution_failed (trace_id=%s): %r",
                trace_id, exc,
            )
            _emit(_failed("invoke_com_error"))
        except Exception as exc:  # noqa: BLE001 -- contract: never raise
            logger.error(
                "click_element: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            _emit(_failed("invoke_com_error"))

    def retry_dictation_by_token(
        self,
        correlation_token: str = "",
        override_strategy: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Replay a rejected dictation through ClipboardOnlyStrategy (wh-ftg63).

        Logic process forwards a ``retry_dictation_by_token`` request when
        the user clicks "Try it anyway" on a text-target rejection toast
        (Phase 4 of wh-9weum). The request carries only the
        correlation_token and override_strategy -- no dictation text. This
        handler:

          1. Validates the request shape via the schema; on schema error
             returns an ``unknown_token`` response (graceful degrade per
             wh-uf54). The text never crosses processes; the schema
             rejects any payload that tries.
          2. Resolves the correlation_token in the input-process
             rejection-text cache. The cache's three-way ``resolve()``
             method maps directly onto the contract's three statuses:
                - HIT     -> run ClipboardOnlyStrategy, return success.
                - MISS    -> ``unknown_token`` (token never cached or
                             evicted under max_entries pressure).
                - EXPIRED -> ``token_expired`` (TTL elapsed).
          3. On HIT, dispatches the cached text through
             ``self.clipboard_only_strategy.insert`` directly. The router
             is bypassed: ``override_strategy='clipboard_only'`` is the
             contract's way of forcing the soft-fallback path regardless
             of the predicate's verdict.
          4. Emits exactly one response on the response queue, parsed
             via ``RetryDictationByTokenResponse.to_dict()`` and
             augmented with ``request_id`` and ``action`` so the demuxer
             in ``app.py`` can resolve the awaiting Future.

        Privacy property (wh-x4mv.2 round 2): the cached dictation text
        leaves Input only as the clipboard write performed by
        ClipboardOnlyStrategy. It does NOT appear in the response
        payload, in any log line emitted from this method, or in any
        other IPC message. Only the correlation_token is logged for
        diagnostics.
        """
        # Imported lazily to avoid a top-level import cycle through the
        # services namespace.
        from services.wheelhouse.shared.retry_dictation_by_token import (
            RetryDictationByTokenRequest,
            RetryDictationByTokenResponse,
            RetryDictationByTokenSchemaError,
        )
        from .rejection_text_cache import CacheStatus

        action_name = "retry_dictation_by_token"

        def _emit(response: "RetryDictationByTokenResponse") -> None:
            payload = response.to_dict()
            if request_id is not None:
                payload["request_id"] = request_id
            payload["action"] = action_name
            try:
                self.response_queue.put(payload)
            except Exception as exc:
                # Last-resort: log and drop. Without a response, the
                # logic-side Future will time out, which logic already
                # handles gracefully (it surfaces the same follow-up
                # toast as token_expired).
                logger.error(
                    "retry_dictation_by_token: failed to enqueue response "
                    "for token=%s: %s",
                    correlation_token, exc,
                )

        # Step 1: schema validation. We rebuild the action-payload shape
        # the schema expects so a single validator covers correlation_token
        # uuid4 shape AND override_strategy allowlist.
        try:
            RetryDictationByTokenRequest.from_action_payload(
                {
                    "action": action_name,
                    "params": {
                        "correlation_token": correlation_token,
                        "override_strategy": override_strategy,
                    },
                }
            )
        except RetryDictationByTokenSchemaError as exc:
            logger.warning(
                "retry_dictation_by_token: dropping malformed request "
                "(token=%s): %s",
                correlation_token, exc,
            )
            _emit(RetryDictationByTokenResponse.unknown_token(
                reason="schema_error",
            ))
            return

        # Step 2: cache lookup with three-way outcome.
        result = self.rejection_text_cache.resolve(correlation_token)

        if result.status is CacheStatus.MISS:
            logger.debug(
                "retry_dictation_by_token: cache MISS for token=%s",
                correlation_token,
            )
            _emit(RetryDictationByTokenResponse.unknown_token())
            return

        if result.status is CacheStatus.EXPIRED:
            logger.debug(
                "retry_dictation_by_token: cache EXPIRED for token=%s",
                correlation_token,
            )
            _emit(RetryDictationByTokenResponse.token_expired())
            return

        # Step 3: cache HIT -- run ClipboardOnlyStrategy directly,
        # bypassing the router. The cached text is held in a local
        # variable that is NOT logged.
        cached_text = result.text or ""

        try:
            # wh-override-paste-focus-drift: restore foreground to the
            # originally-rejected target BEFORE capture_context() runs.
            # The click on the toast button moves focus to the toast's
            # own QPushButton; without this refocus, capture_context()
            # sees the QPushButton and ClipboardOnlyStrategy pastes
            # into the toast button, which silently consumes the
            # keystroke. The cache stores target_hwnd=0 when the
            # rejection-time HWND lookup failed (stale COM, no
            # top-level); such an entry is refused outright below
            # (.1.11) -- it never reaches the refocus.
            target_hwnd = result.target_hwnd
            target_pid = result.target_process_id
            # wh-ensure-focused-same-process-fallback.1.11: an entry
            # with target_hwnd=0 carries no target identity at all --
            # every guard below sits inside an ``if target_hwnd:``
            # block, so such an entry used to skip the PID check, the
            # root gates, AND the refocus, then paste into whatever
            # window held foreground at click time (the original
            # wh-override-paste-focus-drift toast-button failure,
            # resurrected for exactly the entries whose target is
            # least known). Refuse outright instead: the rejection-time
            # HWND lookup failed, so no later probe can prove where
            # this paste would land.
            if not target_hwnd:
                logger.info(
                    "retry_dictation_by_token: cache entry has no "
                    "target hwnd; emitting token_expired",
                )
                _emit(RetryDictationByTokenResponse.token_expired(
                    reason="target_window_gone",
                ))
                return
            if target_hwnd and target_pid:
                # wh-override-paste-focus-drift.1.2: detect HWND reuse
                # before refocusing. Windows reassigns HWND values to
                # new windows when the original closes; a stale HWND
                # whose PID no longer matches the rejection-time PID
                # may name an unrelated window and pasting the cached
                # dictation into it would silently leak text. A
                # GetWindowThreadProcessId return of (0, 0) means the
                # HWND no longer names any window. Both cases emit
                # token_expired so the GUI surfaces the canonical
                # follow-up wording.
                try:
                    _tid, live_pid = (
                        win32process.GetWindowThreadProcessId(target_hwnd)
                    )
                except Exception as exc:
                    logger.debug(
                        "retry_dictation_by_token: "
                        "GetWindowThreadProcessId(hwnd=%s) raised: %s; "
                        "treating as target_window_gone",
                        hex(target_hwnd), exc,
                    )
                    live_pid = 0
                if live_pid != target_pid:
                    logger.info(
                        "retry_dictation_by_token: target window gone "
                        "(hwnd=%s cached_pid=%d live_pid=%d); "
                        "emitting token_expired",
                        hex(target_hwnd), target_pid, live_pid,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
            # wh-ensure-focused-same-process-fallback.1.7: the PID
            # guard above cannot see a SAME-process handle recycle.
            # Brave runs every window from one browser process, so a
            # destroyed helper HWND reborn as a child of another Brave
            # top-level window keeps the cached PID -- and
            # ensure_focused's normalized strict compare then maps the
            # recycled handle to that other window's root and credits
            # it, pasting the cached dictation into a window the user
            # never dictated into. (The pre-normalization raw equality
            # refused this case by accident.) Compare the live
            # GA_ROOT of the cached handle against the rejection-time
            # snapshot; fail closed when it drifted or cannot be
            # resolved.
            # wh-ensure-focused-same-process-fallback.1.8: a nonzero
            # HWND with target_root=0 (rejection-time normalization
            # failed) refuses outright rather than skipping the
            # comparison. The cache lives only in Input-process
            # memory, so no persisted legacy entry needs a skip path,
            # and skipping would recreate the .1.7 sequence for
            # exactly the entries whose rejection-time identity is
            # least known.
            target_root = result.target_root
            target_tag = result.target_tag
            if target_hwnd:
                if not target_root:
                    logger.info(
                        "retry_dictation_by_token: no rejection-time "
                        "root snapshot for hwnd=%s; emitting "
                        "token_expired",
                        hex(target_hwnd),
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
                live_root = normalize_hwnd_for_foreground_compare(
                    target_hwnd,
                )
                if live_root != target_root:
                    logger.info(
                        "retry_dictation_by_token: target root drifted "
                        "(hwnd=%s cached_root=%s live_root=%s); "
                        "emitting token_expired",
                        hex(target_hwnd), hex(target_root),
                        hex(live_root) if live_root else live_root,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
                # wh-ensure-focused-same-process-fallback.1.12: every
                # guard above compares numeric handle values, and a
                # handle recycled as a NEW same-PID top-level window
                # that is its own GA_ROOT aliases them all --
                # normalize(hwnd) returns the handle itself (matching
                # a snapshot taken the same way), the PID matches,
                # and ensure_focused's strict compare then credits
                # the impostor before any shape probe runs. The
                # rejection-time SetProp marker lives on the window
                # OBJECT and dies with it, so re-reading the property
                # is the one probe a SAME-RUN recycle cannot alias:
                # the new object reads 0, or a survivor marker from
                # an earlier Input-process run that matches only on
                # equal 43-bit salts (about 2**-43 per pair of runs,
                # the accepted residual documented at _RUN_SALT in
                # ui/hwnd_utils.py). A stored tag of 0 (SetProp failed
                # at rejection time -- destroyed handle or a
                # UIPI-protected elevated window) refuses outright,
                # mirroring the .1.8 root-gate contract.
                if not target_tag:
                    logger.info(
                        "retry_dictation_by_token: no rejection-time "
                        "provenance tag for hwnd=%s; emitting "
                        "token_expired",
                        hex(target_hwnd),
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
                live_tag = read_hwnd_provenance(target_hwnd)
                if live_tag != target_tag:
                    logger.info(
                        "retry_dictation_by_token: provenance tag "
                        "mismatch (hwnd=%s cached_tag=%d live_tag=%d); "
                        "window object was recycled; emitting "
                        "token_expired",
                        hex(target_hwnd), target_tag, live_tag,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
            # wh-review-pattern-fixes.8: this direct ClipboardOnlyStrategy
            # dispatch bypasses the router, and the strategy never touches
            # the shadow buffer. Invalidate before the refocus + paste so a
            # partial failure (focus moved, paste unverified) also leaves
            # the buffer invalid (see the hotkey_action block comment).
            self.buffer_manager.invalidate()
            if target_hwnd:
                refocused = self.window_manager.ensure_focused(target_hwnd)
                if not refocused:
                    # wh-override-retry-fail-open-leak: with the GUI's
                    # AllowSetForegroundWindow grant in place (round 2
                    # of wh-override-paste-focus-drift), ensure_focused
                    # returns False only when the target is genuinely
                    # unreachable -- closed, minimized, or hidden in a
                    # way Windows refuses to override even with the
                    # grant. Pasting anyway would send Ctrl+V to
                    # whatever holds foreground at that moment,
                    # leaking the cached dictation into an unrelated
                    # control. Fail closed and emit token_expired so
                    # the GUI surfaces the canonical follow-up wording.
                    logger.info(
                        "retry_dictation_by_token: ensure_focused(hwnd=%s) "
                        "returned False; emitting token_expired",
                        hex(target_hwnd),
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
                # wh-ensure-focused-same-process-fallback.1.9: the
                # pre-refocus root check above cannot cover the
                # refocus interval -- Windows can recycle the handle
                # as a child of a same-process sibling DURING
                # ensure_focused, whose strict normalized compare
                # then credits the sibling. Re-normalize after the
                # successful refocus, immediately before
                # capture_context, so the paste stands only while the
                # handle still names the rejection-time root. The
                # .1.8 gate guarantees target_root is nonzero here.
                # No Win32 sequence makes this atomic with the
                # capture and paste; this narrows the interval to a
                # single probe.
                post_root = normalize_hwnd_for_foreground_compare(
                    target_hwnd,
                )
                if post_root != target_root:
                    logger.info(
                        "retry_dictation_by_token: target root drifted "
                        "during refocus (hwnd=%s cached_root=%s "
                        "post_root=%s); emitting token_expired",
                        hex(target_hwnd), hex(target_root),
                        hex(post_root) if post_root else post_root,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
                # wh-ensure-focused-same-process-fallback.1.12: the
                # root re-check above still compares handle values,
                # so a recycle into a same-PID own-root top-level
                # window DURING ensure_focused passes it (the new
                # window normalizes to the handle itself, equal to a
                # snapshot taken the same way). Re-read the window
                # property too: the recycled object never carried
                # the marker and reads 0. The .1.12 pre-check
                # guarantees target_tag is nonzero here.
                post_tag = read_hwnd_provenance(target_hwnd)
                if post_tag != target_tag:
                    logger.info(
                        "retry_dictation_by_token: provenance tag "
                        "lost during refocus (hwnd=%s cached_tag=%d "
                        "post_tag=%d); window object was recycled; "
                        "emitting token_expired",
                        hex(target_hwnd), target_tag, post_tag,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return

            context = capture_context()
            if target_hwnd:
                # wh-ensure-focused-same-process-fallback.1.16 (codex
                # round 10): every probe above validates the CACHED
                # handle, but ClipboardOnlyStrategy derives its paste
                # target from THIS captured control
                # (specific.py _hwnd_from_control), and capture_context
                # is not a zero-duration gap -- it performs UIA focus
                # resolution, psutil, and top-level-control work. A
                # focus change inside that interval hands the strategy
                # a different window and the cached text pastes there
                # with every cached-target guard already passed. Bind
                # the captured control back to the verified identity:
                # its top-level must normalize to the cached root, and
                # the provenance marker must still be on that
                # top-level (the root comparison alone is a
                # handle-value check, which an own-root recycle
                # aliases). Any resolution failure refuses -- pasting
                # without the proof delivers into whatever
                # capture_context happened to return.
                captured_top = top_level_hwnd_from_control(
                    context.focused_control,
                )
                captured_root = (
                    normalize_hwnd_for_foreground_compare(captured_top)
                    if captured_top else None
                )
                if not captured_root or captured_root != target_root:
                    logger.info(
                        "retry_dictation_by_token: captured control "
                        "resolves outside the verified target "
                        "(cached_root=%s captured_root=%s); emitting "
                        "token_expired",
                        hex(target_root),
                        hex(captured_root) if captured_root
                        else captured_root,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
                final_tag = read_hwnd_provenance(captured_top)
                if final_tag != target_tag:
                    logger.info(
                        "retry_dictation_by_token: provenance tag "
                        "gone from captured top-level (cached_tag=%d "
                        "final_tag=%d); window object was recycled "
                        "during capture; emitting token_expired",
                        target_tag, final_tag,
                    )
                    _emit(RetryDictationByTokenResponse.token_expired(
                        reason="target_window_gone",
                    ))
                    return
            if context.focused_control:
                self.window_manager.remember_target(context.focused_control)

            # wh-soft-allow-verdict-tier.1.1: reset the preceding-chars
            # mirror before each retry replay. Logic leaves the token in
            # the cache after an unverified outcome (the keystroke fired
            # but verification could not confirm delivery) so the user
            # can click Try-it-anyway again. Without this reset, the
            # second click would perfect cached_text against the first
            # click's perfected output, producing a different paste
            # (e.g. cached "hello" pastes as "Hello" first and " hello"
            # second). The retry is conceptually one paste of the
            # cached text starting from a clean slate, so we want the
            # same perfected output every time.
            self.clipboard_only_strategy.reset_preceding_mirror()

            # wh-ensure-focused-same-process-fallback.1.18 (codex
            # round 11): hand the verified cached identity down to the
            # strategy. Every probe above finishes before the strategy
            # re-resolves its paste target from the captured control,
            # and the clipboard verify loop inside verified_paste runs
            # before the Ctrl+V -- the marker must be re-read inside
            # that final interval, which only verified_paste can do.
            retry_identity = (
                (target_hwnd, target_tag) if target_hwnd else None
            )

            # The retry click usually fires after end_utterance has run, so
            # the utterance manager's clipboard restore will not cover us.
            # Mirror the non-utterance branch of intelligent_insert_text
            # (line ~688) and wrap the strategy call in clipboard_context
            # so the user's prior clipboard contents come back. Inside an
            # active utterance, mark_clipboard_dirty + end_utterance handle
            # restore as usual and a second wrap would double-restore.
            if self.utterance_manager.is_in_utterance():
                insertion_result = self.clipboard_only_strategy.insert(
                    cached_text, context, request_id, None,
                    retry_identity=retry_identity,
                )
            else:
                with clipboard_context(restore_delay=0.05):
                    insertion_result = self.clipboard_only_strategy.insert(
                        cached_text, context, request_id, None,
                        retry_identity=retry_identity,
                    )

            # wh-lost-word-neighbour-paths.1.5: both branches above
            # assign insertion_result, so one record covers them.
            self._record_credited_target(context, insertion_result)

            # Forward dirty signal to the utterance manager (parallel to
            # _execute_insert_with_ack at line ~754). The override path
            # may not be inside an active utterance (the click happens
            # outside the speech pipeline) so guard the call.
            if insertion_result.clipboard_dirty:
                try:
                    self.utterance_manager.mark_clipboard_dirty(
                        write_seq=self.clipboard.last_clipboard_write_seq,
                    )
                except Exception:
                    # The mark is advisory; failing to mark must not
                    # prevent the success response from going out.
                    pass

            # ClipboardOnlyStrategy returns success=False only when it
            # refused before sending Ctrl+V (specific.py:1520). In that
            # case nothing landed on screen and the user must see the
            # follow-up toast, so surface a non-success response. The
            # logic-side forwarder already maps any non-success status
            # to the canonical follow-up wording.
            if not insertion_result.success:
                logger.debug(
                    "retry_dictation_by_token: ClipboardOnlyStrategy "
                    "returned success=False for token=%s; emitting "
                    "token_expired(reason=delivery_failed)",
                    correlation_token,
                )
                _emit(RetryDictationByTokenResponse.token_expired(
                    reason="delivery_failed",
                ))
                return

            outcome = insertion_result.retry_outcome
            if outcome not in ("verified", "unverified"):
                # ClipboardOnlyStrategy is contracted to populate
                # retry_outcome on every result (wh-pc28). A surprise
                # value here is a strategy bug; degrade to 'unverified'
                # so the contract holds.
                logger.warning(
                    "retry_dictation_by_token: ClipboardOnlyStrategy "
                    "returned unexpected retry_outcome=%r; coercing to "
                    "'unverified' (token=%s)",
                    outcome, correlation_token,
                )
                outcome = "unverified"

            logger.debug(
                "retry_dictation_by_token: cache HIT for token=%s "
                "outcome=%s success=%s",
                correlation_token, outcome, insertion_result.success,
            )

            # wh-override-multiword-retry: invalidate the cache entry
            # on a verified retry so subsequent rejections against the
            # same target allocate a fresh correlation_token instead
            # of appending onto an already-consumed entry. Logic adds
            # this token to ``consumed_retry_tokens`` on the same
            # verified outcome (main.py:1326) so a duplicate click is
            # silently dropped; if Input keeps appending to the same
            # token, the user's next stretch of dictation would also
            # bind to the consumed token and the next click would be
            # dropped without warning. Unverified retries leave the
            # entry in place so the user can click Try-it-anyway again.
            if outcome == "verified":
                try:
                    self.rejection_text_cache.invalidate(correlation_token)
                except Exception as exc:
                    logger.debug(
                        "retry_dictation_by_token: cache invalidate "
                        "raised for token=%s: %s",
                        correlation_token, exc,
                    )
                # wh-override-multiword-retry.2.2 (deepseek finding):
                # also drop any aggregation bucket on the rejected
                # strategy that points at the now-invalidated token
                # so the bucket map stays synchronised with the cache.
                # Without this call, the stale bucket entry would
                # leak until the next call to _emit_rejection_event
                # happened to trigger _prune_dead_buckets.
                try:
                    self.rejected_strategy.forget_token(correlation_token)
                except Exception as exc:
                    logger.debug(
                        "retry_dictation_by_token: forget_token "
                        "raised for token=%s: %s",
                        correlation_token, exc,
                    )

            _emit(RetryDictationByTokenResponse.success(retry_outcome=outcome))

        except Exception as exc:
            # An unexpected strategy exception: log and degrade to
            # token_expired so logic surfaces the same follow-up toast.
            # We deliberately do NOT include the cached text in the
            # log line; the exception's str() is the strategy's own
            # message and must not echo our text either, but defensive
            # filtering would over-engineer a path that only fires on
            # an unhandled bug.
            logger.error(
                "retry_dictation_by_token: ClipboardOnlyStrategy raised "
                "for token=%s: %s",
                correlation_token, exc, exc_info=True,
            )
            _emit(RetryDictationByTokenResponse.token_expired(
                reason="strategy_error",
            ))

    def press_key_action(self, key: str, repeat: int = 1, **kwargs):
        """Press a key. Every press invalidates the shadow buffer.

        Args:
            key: Key to press
            repeat: Number of times to repeat
        """
        if 'request_id' in kwargs:
            logger.warning(f"Unexpected 'request_id' in press_key_action for key '{key}'")

        # Review wh-review-pattern-fixes.5: invalidate unconditionally,
        # mirroring hotkey_action (wh-space-after-hotkey-linebreak). The
        # target application decides what a pressed key does -- a letter,
        # a digit, space, or punctuation types content and moves the
        # caret, and the keyboard listener ignores WheelHouse's own
        # synthetic input while an internal action runs. The old
        # CACHE_INVALIDATING_KEYS allowlist missed every content-typing
        # key ("press a", "press 5", "press space", "press ."), leaving
        # the next dictated word to perfect against pre-press context.
        # The cost of an unnecessary invalidation is one UIA re-sync on
        # the next dictated word.
        self.buffer_manager.invalidate()

        # wh-keyboard-refusal-notice: decided inside the try, acted on after
        # it, so a notice that itself fails cannot land the handler in the
        # except branch and show a SECOND notice. Same shape as scroll_wheel.
        refusal: Optional[str] = None
        try:
            if key.lower() == "enter" and self.terminal_editor.is_active:
                self.terminal_editor.submit()
                return

            # Capture Context for Flutter detection
            context = capture_context()
            _stop_command_on_failed_focus_read(context, "press_key_action")
            is_flutter = context.is_flutter
            focused_control = context.focused_control

            # Flutter apps filter SendInput for special keys, so use press_keys instead
            # SendKeys doesn't support special keys (only text input)
            if is_flutter:
                logger.debug(f"[FLUTTER] press_key_action: key='{key}' - using press_keys (SendInput) for special key")
            
            for _ in range(repeat):
                ok, accepted, expected = verified_press_keys(
                    key, caller_notifies=True
                )
                if not ok:
                    refusal = self._key_refusal_message(
                        (key,), expected,
                        PRESS_KEY_UNKNOWN_MESSAGE, PRESS_KEY_REFUSED_MESSAGE,
                    )
                    if accepted:
                        # A short send can leave the key physically held
                        # (down accepted, up dropped), so the notice
                        # would report a failure while Windows
                        # auto-repeats the key into the focused
                        # application. Release it first, the way
                        # press_key_verified below does (wh-eolas.2.5).
                        # An unmapped key name reports (False, 0, 0) and
                        # sent nothing, so it has nothing to release.
                        _send_modifier_keyups((key,))
                    logger.warning(
                        "press_key_action: key=%r refused (sent %d/%d)",
                        key, accepted, expected,
                    )
                    break
        except Exception as e:
            # WARNING, not ERROR, for the reason scroll_wheel records: the
            # notice below is the report, and an ERROR record would show a
            # second box beside it through ErrorNotificationHandler.
            logger.warning(f"Error pressing key '{key}': {e}", exc_info=True)
            refusal = PRESS_KEY_REFUSED_MESSAGE.format(keys=key)
        if refusal is not None:
            self._say_the_keyboard_refused(refusal, source="press key")

    def press_key_verified(self, key: str, repeat: int = 1, **kwargs) -> dict:
        """Press a key with SendInput delivery verification.

        wh-review-pattern-fixes.32 (c): ``press_key_action`` delivers
        through ``press_keys``, which discards the SendInput count, and
        callers reach it over the enqueue-only ``send_command`` channel
        -- so Logic can never learn whether a state-changing press
        landed. This variant sends through ``verified_press_keys`` and
        returns ``{"success": bool}`` for the request/response channel
        (input_proc puts the returned dict on the response queue, the
        same special-case shape ``capture_selected_text`` uses).

        Used by the AI transform's fallback-selection collapse, where a
        silently dropped Right leaves a whole-field selection armed
        while Logic believes cleanup completed.

        Always sends via SendInput (no Flutter / terminal-editor
        special cases: the callers press plain caret keys). Same
        unconditional shadow-buffer invalidation as press_key_action.
        """
        self.buffer_manager.invalidate()
        try:
            for _ in range(repeat):
                ok, accepted, expected = verified_press_keys(key)
                if not ok:
                    # A short send can leave the key physically held
                    # (down accepted, up dropped) -- release it before
                    # reporting (wh-eolas.2.5 shape).
                    _send_modifier_keyups((key,))
                    logger.error(
                        "press_key_verified: short SendInput for "
                        "key=%r (sent %d/%d); reporting failure.",
                        key, accepted, expected,
                    )
                    return {"success": False}
        except Exception as e:
            logger.error(
                f"Error in press_key_verified for key '{key}': {e}",
                exc_info=True,
            )
            return {"success": False}
        return {"success": True}

    def hotkey_action(self, keys: list, repeat: int = 1, **kwargs):
        """Execute a hotkey combination, optionally repeated.

        Args:
            keys: List of keys to press together
            repeat: Number of times to repeat the hotkey (default: 1)
        """
        if 'request_id' in kwargs:
            logger.warning(f"Unexpected 'request_id' in hotkey_action for keys '{keys}'")

        # wh-space-after-hotkey-linebreak: every hotkey invalidates the
        # shadow buffer. A hotkey is a command, not dictation, and it can
        # move the caret or rewrite the target text in ways WheelHouse
        # cannot model from the key list alone (shift+enter, ctrl+v,
        # ctrl+z, ctrl+a, ctrl+home). The remembered copy of the target
        # text is therefore unusable after any hotkey.
        #
        # The keyboard listener cannot cover this gap. input_proc.py calls
        # _should_emit_keyboard_invalidation with is_internal_action set
        # for the whole duration of a dispatched action, and that function
        # returns False for internal actions, which is exactly when these
        # keys are sent. So the invalidation has to happen here.
        #
        # The reported failure: the 'new paragraph' pattern sends
        # shift+enter twice through this method. The buffer stayed valid,
        # VerifiedUnicodeStrategy skipped its re-sync, get_context()
        # returned the two characters before the pre-hotkey caret, and
        # TextPerfector added a space in front of the next dictated word.
        # The user saw a line that started with a space.
        #
        # press_key_action now invalidates unconditionally for the same
        # reason (wh-review-pattern-fixes.5): a modifier combination or
        # the target application changes what an otherwise harmless key
        # does. The cost of an unnecessary invalidation is one UIA
        # re-sync on the next dictated word.
        self.buffer_manager.invalidate()

        # Decided inside the try, acted on after it: see press_key_action.
        refusal: Optional[str] = None
        try:
            normalized_keys = [str(k).lower() for k in keys]
            if self.terminal_editor.is_active and normalized_keys == ["enter"]:
                self.terminal_editor.submit()
                return

            # Capture Context for Flutter detection
            context = capture_context()
            _stop_command_on_failed_focus_read(context, "hotkey_action")
            is_flutter = context.is_flutter
            focused_control = context.focused_control

            if is_flutter and focused_control and focused_control.Exists(0, 0):
                # Convert keys to SendKeys format (e.g., ['ctrl', 'c'] -> '{Ctrl}c')
                sendkeys_str = self._convert_to_sendkeys_format(keys)
                logger.debug(f"Flutter hotkey: {sendkeys_str} (repeat={repeat})")
                for _ in range(repeat):
                    focused_control.SendKeys(sendkeys_str)
            else:
                # Standard SendInput for non-Flutter apps
                for _ in range(repeat):
                    ok, accepted, expected = verified_press_keys(
                        *keys, caller_notifies=True
                    )
                    if not ok:
                        refusal = self._key_refusal_message(
                            tuple(keys), expected,
                            HOTKEY_UNKNOWN_MESSAGE, HOTKEY_REFUSED_MESSAGE,
                        )
                        if accepted:
                            # A short chord can leave a key physically
                            # held (down accepted, up dropped), so a held
                            # ctrl would run every later keystroke as a
                            # shortcut. Release it first, the way
                            # press_key_verified does (wh-eolas.2.5). An
                            # unmapped key name reports (False, 0, 0) and
                            # sent nothing, so it has nothing to release.
                            _send_modifier_keyups(tuple(keys))
                        logger.warning(
                            "hotkey_action: keys=%r refused (sent %d/%d)",
                            keys, accepted, expected,
                        )
                        break
        except Exception as e:
            # WARNING, not ERROR: see press_key_action.
            logger.warning(
                f"Error executing hotkey '{keys}' (repeat={repeat}): {e}",
                exc_info=True,
            )
            # The join iterates keys a SECOND time, and the commonest way
            # to reach this arm is a keys value the FIRST iteration could
            # not read. input_proc validates only the container before
            # method_to_call(**params), so nothing between Logic and here
            # rejects a keys value that is not a list; no producer in this
            # repository emits one today (grep for hotkey_action under
            # services/wheelhouse excluding .venv and tests: every one
            # builds a list of strings). A raise here would carry the
            # exception out of the method and leave the user with the
            # rate-limited generic [ERROR] box this bead removes
            # (wh-keyboard-refusal-notice.1.2).
            try:
                chord = " + ".join(str(k) for k in keys)
            except TypeError:
                chord = str(keys)
            refusal = HOTKEY_REFUSED_MESSAGE.format(chord=chord)
        if refusal is not None:
            self._say_the_keyboard_refused(refusal, source="hotkey")
    
    def scroll_wheel(self, direction=None, clicks: int = 1, **kwargs):
        """Turn the mouse wheel for a spoken scroll command.

        Shaped like :meth:`hotkey_action`, NOT like the mouse-grid pointer
        handlers, on three counts:

        1. It is not in ``_HANDLES_OWN_RESPONSE`` and emits no
           ``MouseActionResponse``. A scroll command is a fire-and-forget step
           in a pattern's action list, and the generic dispatcher in
           input_proc.py answers any waiting Logic request for it.
        2. It is not gated by the voice-clicking config. That gate exists for
           the grid's pointer actions, which move the physical pointer and
           click with it. A wheel notch presses nothing and moves nothing, and
           a user who turned voice clicking off still has to be able to read a
           long page.
        3. It never raises. Argument validation lives in the SendInput
           primitive, which reports ``invalid_direction`` / ``invalid_clicks``
           rather than raising, so a malformed IPC message is logged and
           dropped instead of breaking the Input process command loop.

        Args:
            direction: ``"up"``, ``"down"``, ``"left"`` or ``"right"``.
            clicks: whole wheel notches; the primitive caps the range.
        """
        # A wheel notch over a combo box, a spinner or a slider changes that
        # control's VALUE rather than scrolling a view, so the remembered copy
        # of the focused control's text cannot survive it. The keyboard
        # listener never sees a wheel event, so the invalidation has to happen
        # here -- the same reasoning the hotkey_action block comment records.
        self.buffer_manager.invalidate()

        # wh-voice-access-parity.2.3.3: one scroll at a time. A user who says
        # "scroll down 3" while a continuous scroll is running asked for three
        # notches, not for three notches added to a scroll that keeps going.
        # This runs BEFORE the notches are sent, and its failure never costs
        # them: the notches are what the user asked for.
        self._stop_continuous_scroll("a discrete scroll command arrived")

        # wh-wheel-refusal-notice: decided inside the try, acted on after it,
        # so a notice that itself fails cannot land the handler in the except
        # branch and show a SECOND notice.
        refused = False
        try:
            succeeded, reason = self._mouse_scroll_seam(direction, clicks)
            logger.info(
                "scroll_wheel: direction=%r clicks=%r ok=%s reason=%s",
                direction, clicks, succeeded, reason,
            )
            refused = (
                not succeeded and reason not in _WHEEL_VALIDATION_REFUSALS
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            # WARNING, not ERROR: the notice below is the one the user should
            # get, and an ERROR record would show a second box beside it
            # through ErrorNotificationHandler (wh-wheel-refusal-notice).
            logger.warning(
                "scroll_wheel: unexpected error (direction=%r clicks=%r): %s",
                direction, clicks, exc, exc_info=True,
            )
            refused = True
        if refused:
            self._say_the_wheel_would_not_turn()

    def _say_the_wheel_would_not_turn(self) -> None:
        """Tell the user a discrete scroll did nothing, and never raise.

        wh-wheel-refusal-notice: before this the discrete spoken scroll had no
        notice of its own, and the only thing the user saw was the generic
        ``[ERROR]`` box ``ErrorNotificationHandler`` shows for every ERROR
        record. The wheel primitive's refusal record is a WARNING now, so this
        is the whole of what the user gets.

        Reasons NOT listed in ``_WHEEL_VALIDATION_REFUSALS`` reach here,
        including a reason name added later: a new refusal that shows nothing
        at all is the worse failure of the two.
        """
        try:
            send_notice(
                NOTICE_TITLE, SCROLL_REFUSED_MESSAGE,
                source="discrete scroll",
            )
        except Exception as exc:  # noqa: BLE001 -- a report must never raise
            # WARNING, not ERROR, because an ERROR here pops its own
            # notification -- the duplicate this bead exists to remove.
            logger.warning(
                "scroll_wheel: the refusal notice failed: %s",
                type(exc).__name__,
            )

    def _key_refusal_message(
        self, keys: tuple, expected: int, unknown: str, refused: str,
    ) -> str:
        """Word the notice for a refused key send (wh-keyboard-refusal-notice).

        ``verified_press_keys`` reports an unrecognized key name as
        ``(False, 0, 0)`` and a short send as ``(False, accepted, expected)``
        with ``expected`` above zero, so ``expected == 0`` is what separates
        the two causes. It never says WHICH name it did not know, so this
        reads :data:`VK_CODE_MAP` and names them (boss ruling B, recorded on
        wh-keyboard-refusal-notice).

        Falls back to the short-send wording when no name is unrecognized.
        A notice that named nothing would be worse than one that names the
        chord.
        """
        chord = " + ".join(str(k) for k in keys)
        if expected == 0:
            unrecognized = [
                str(k) for k in keys if str(k).lower() not in VK_CODE_MAP
            ]
            if unrecognized:
                return unknown.format(
                    chord=chord, keys=", ".join(unrecognized)
                )
        return refused.format(chord=chord, keys=chord)

    def _say_the_keyboard_refused(self, message: str, *, source: str) -> None:
        """Tell the user a keyboard action did nothing, and never raise.

        The same notice path the wheel refusal fix uses
        (:meth:`_say_the_wheel_would_not_turn`), for the same reason: the
        generic ``[ERROR]`` box cannot be relied on to reach the user,
        because ``ErrorNotificationHandler.emit`` drops a repeat of the same
        (logger, level, message) inside ``rate_limit_seconds``, which
        ``utils/logging_setup.py`` sets to 10 (wh-wheel-refusal-notice.1.7).
        ``send_notice`` does not go through that handler at all, so two
        identical failures inside ten seconds show two notices.
        """
        try:
            send_notice(NOTICE_TITLE, message, source=source)
        except Exception as exc:  # noqa: BLE001 -- a report must never raise
            # WARNING, not ERROR, because an ERROR here pops its own
            # notification -- the duplicate this bead exists to remove.
            logger.warning(
                "%s: the refusal notice failed: %s",
                source, type(exc).__name__,
            )

    def _turn_the_wheel(self, direction, clicks):
        """The wheel call the continuous scroller makes each tick.

        It reads ``_mouse_scroll_seam`` at call time rather than closing over
        it, so replacing the handler's seam reaches the running timer too.
        """
        return self._mouse_scroll_seam(direction, clicks)

    def _stop_continuous_scroll(self, why: str) -> None:
        """Stop the continuous scroll, if one is running, and never raise.

        Every caller is a handler that has other work to finish, so a failure
        here is logged and the caller carries on (wh-voice-access-parity.2.3.3).
        """
        try:
            stopped = self._continuous_scroller.stop()
            if stopped:
                logger.info("continuous scroll stopped because %s", why)
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "stopping the continuous scroll failed (%s): %s",
                why, exc, exc_info=True,
            )

    def start_continuous_scroll(self, direction=None, **kwargs):
        """Start a scroll that keeps going (wh-voice-access-parity.2.3.3).

        Shaped like :meth:`scroll_wheel` on every count that handler documents:
        it is not in ``_HANDLES_OWN_RESPONSE``, it is not gated by the
        voice-clicking config, and it never raises.

        It adds one rule of its own. It RETURNS IMMEDIATELY. The Input process
        runs one command loop, and the stop word arrives through that same
        loop, so a handler that scrolled in place would hold the loop for the
        whole scroll and the scroll could never be stopped by voice. The
        repeating timer therefore runs on its own thread
        (ui/continuous_scroll.py) and this handler only starts it.

        An unusable direction starts nothing. The scroller reports that as
        False rather than raising, so a malformed message is logged and
        dropped instead of breaking the command loop.

        Args:
            direction: ``"up"``, ``"down"``, ``"left"`` or ``"right"``.
        """
        # Same reason as the discrete handler: a wheel notch can change a
        # control's value, so the remembered copy cannot survive it.
        self.buffer_manager.invalidate()

        try:
            started = self._continuous_scroller.start(direction)
            logger.info(
                "start_continuous_scroll: direction=%r started=%s",
                direction, started,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "start_continuous_scroll: unexpected error (direction=%r): %s",
                direction, exc, exc_info=True,
            )

    def stop_continuous_scroll(self, **kwargs):
        """Stop the scroll that keeps going (wh-voice-access-parity.2.3.3).

        Takes no direction: stopping is the same act whichever way the scroll
        was going. Stopping when nothing is running is not an error and is not
        reported as one -- a user who says the words twice must not be
        refused, and input_proc.py calls this on a stop command it drops as
        expired, where nothing may be running at all.
        """
        self._stop_continuous_scroll("the stop command arrived")

    def _convert_to_sendkeys_format(self, keys: list) -> str:
        """Convert press_keys format to SendKeys format.
        
        Examples:
            ['ctrl', 'c'] -> '{Ctrl}c'
            ['ctrl', 'shift', 'left'] -> '{Ctrl}{Shift}{Left}'
            ['ctrl', 'a'] -> '{Ctrl}a'
        
        Args:
            keys: List of key names in press_keys format
            
        Returns:
            SendKeys format string
        """
        # Map common modifiers
        modifier_map = {
            'ctrl': 'Ctrl',
            'shift': 'Shift',
            'alt': 'Alt',
            'win': 'Win'
        }
        
        # Map special keys that need braces
        special_keys = {
            'left': 'Left', 'right': 'Right', 'up': 'Up', 'down': 'Down',
            'home': 'Home', 'end': 'End', 'pageup': 'PgUp', 'pagedown': 'PgDn',
            'delete': 'Del', 'backspace': 'BS', 'enter': 'Enter',
            'tab': 'Tab', 'escape': 'Esc', 'space': ' '
        }
        
        result = []
        for key in keys:
            key_lower = key.lower()
            if key_lower in modifier_map:
                result.append(f"{{{modifier_map[key_lower]}}}")
            elif key_lower in special_keys:
                mapped = special_keys[key_lower]
                result.append(f"{{{mapped}}}" if mapped != ' ' else mapped)
            else:
                # Single character key (like 'a', 'c', etc.)
                result.append(key.lower())
        
        return ''.join(result)

    # ========================================================================
    # PUBLIC API - Notifications
    # ========================================================================

    def show_notification(self, title: str, message: str, timeout: int = 5):
        """Display a desktop notification.

        Args:
            title: Notification title
            message: Notification message
            timeout: Display duration in seconds
        """
        try:
            # wh-notice-length-guard: send_measured_notice measures both
            # text fields against the fixed-size arrays plyer writes
            # them into. It imports plyer itself, inside the call, so a
            # missing plyer still reaches the handler below.
            #
            # The alias is load-bearing. This module already imports a
            # different function named send_notice, from
            # .continuous_scroll, whose signature is
            # (title, message, *, source). Two functions of one name in
            # one module is a trap: a later edit that moves this call
            # out of this method would silently bind the other one, and
            # app_name= and timeout= would raise TypeError there.
            from utils.notice_text import send_notice as send_measured_notice

            # Validate parameters
            if not isinstance(title, str) or not isinstance(message, str) or not isinstance(timeout, int):
                logger.error(
                    f"Invalid parameter types: title={type(title)}, "
                    f"message={type(message)}, timeout={type(timeout)}"
                )
                return

            try:
                if not send_measured_notice(
                    title,
                    message,
                    app_name='Wheelhouse',
                    timeout=timeout,
                ):
                    # Before the guard, a missing plyer raised here and
                    # the outer handler wrote "Failed to show
                    # notification". The guard returns False instead of
                    # raising, so without this branch the loss would be
                    # recorded nowhere at this site.
                    logger.warning(
                        "Notification service unavailable; "
                        "notification not shown. Title: %s", title,
                    )
            except Exception as e:
                logger.error(f"Notification dispatch failed: {e}")
        except Exception as e:
            logger.error(f"Failed to show notification: {e}", exc_info=True)
    # ========================================================================
    # PUBLIC API - AI Clipboard Operations
    # ========================================================================

    def _remember_capture_target(self) -> tuple[str, Optional[int]]:
        """Record the control the next capture reads from, and issue a token.

        wh-review-pattern-fixes.45: the AI correction flow crosses a
        process boundary. The capture runs in the Input process, the
        Logic process awaits the model, and the replacement comes back
        as a separate request. A UIA control object cannot travel
        between processes, so the control stays here, in a single slot,
        under a token. Only two plain values go to the Logic process:
        the token and the root-normalized top-level HWND, both of which
        pickle. ``replace_selected_text`` requires both back and refuses
        to send when either fails to match what this slot holds.

        Returns the ``(token, hwnd)`` pair to report to the caller.
        ``hwnd`` is None when the focused control could not be resolved
        to a window; the replacement then has nothing to prove against
        and refuses to send.
        """
        token = uuid.uuid4().hex
        try:
            control = capture_context().focused_control
        except Exception as e:
            logger.error(
                "capture_selected_text: could not capture the target "
                "context: %s; the replacement will have nothing to "
                "prove against.", e,
            )
            control = None
        hwnd = _captured_hwnd_from_control(control)
        self._captured_selection_target = (
            token, CapturedTarget(control=control, hwnd=hwnd),
        )
        return token, hwnd

    def capture_selected_text(self) -> dict:
        """Capture the selection and report the target it came from.

        wh-review-pattern-fixes.45: the capture records the control that
        holds the selection before it touches the clipboard, then adds
        two keys to every result: ``capture_token`` (the identifier the
        Input process resolves back to that control) and ``target_hwnd``
        (the root-normalized top-level window handle). The Logic process
        hands both back to ``replace_selected_text``, which refuses to
        paste when the foreground has drifted away from them.

        Every other key comes from ``_capture_selected_text_body`` and
        is documented there.
        """
        token, target_hwnd = self._remember_capture_target()
        result = self._capture_selected_text_body()
        result["capture_token"] = token
        result["target_hwnd"] = target_hwnd
        return result

    def _capture_selected_text_body(self) -> dict:
        """Capture selected text via clipboard for AI text correction.

        Uses the sentinel clipboard pattern (proven in transform_selection):
        1. Set sentinel on clipboard
        2. Send Ctrl+C to copy selection
        3. Poll clipboard until it changes from sentinel
        4. If unchanged (no selection), send Ctrl+A then Ctrl+C to select all
        5. Return captured text

        Clipboard is saved and restored via clipboard_context.

        Returns:
            Dict with five keys. "text" holds the captured text (empty
            string if none). "flush_failed" is True when the pre-capture
            letter-buffer flush failed; the clipboard and the selection
            were then left untouched (wh-review-pattern-fixes.26).
            "select_all_fallback" is True when the Ctrl+A no-selection
            fallback fired; the whole-field selection it arms survives
            the copy, and the caller owns its cleanup
            (wh-review-pattern-fixes.28). "copy_failed" is True when a
            Ctrl+C chord short-delivered; the selection state is then
            UNKNOWN, not empty, and the caller must fail the operation
            instead of treating the result as a no-selection capture
            (wh-review-pattern-fixes.35). "capture_failed" is True on
            EVERY exit where the capture did not complete -- including
            the two above that already carry a specific flag
            (wh-review-pattern-fixes.38). Four exits used to return
            text='' with both specific flags False, which is
            byte-identical to a genuine empty selection, so the Logic
            side spoke the ordinary no-text message although the Input
            process had failed before it could tell whether anything
            was selected. A caller that wants specific wording checks
            the specific flag first; "capture_failed" is the catch-all
            no caller can forget. A genuine empty selection is the only
            empty result with "capture_failed" False.
        """
        captured = ""
        select_all_fallback = False
        capture_failed = False
        try:
            with clipboard_context(restore_delay=0.05):
                # wh-review-pattern-fixes.26 (sibling of the
                # transform_selection / wrap_or_insert selection
                # branches): a single letter deferred earlier in this
                # utterance can still sit in _letter_buffer, and the
                # AI transform runs at the utterance boundary BEFORE
                # the deferred end_utterance flush. Flush BEFORE the
                # sentinel write and any Ctrl+C / Ctrl+A, so the
                # capture sees the letter on screen. Fail closed
                # (finding .23 design): on a failed flush, touch
                # neither the clipboard nor the selection and report
                # the failure to the caller.
                if self._letter_buffer:
                    if not self._flush_letter_buffer():
                        logger.warning(
                            "capture_selected_text: letter-buffer flush "
                            "failed; clipboard and selection not touched."
                        )
                        return {
                            "text": "",
                            "flush_failed": True,
                            "select_all_fallback": False,
                            "copy_failed": False,
                            "capture_failed": True,
                        }

                sentinel = f"__SENTINEL__{time.time()}"
                # wh-fz7j.4: route through _safe_copy for seq tracking.
                # wh-fz7j.5: bail out if the sentinel write fails; otherwise
                # _poll_clipboard would compare against a sentinel that was
                # never copied and could return stale clipboard text.
                if not self.clipboard._safe_copy(sentinel):
                    # wh-review-pattern-fixes.38: no Ctrl+C was ever
                    # sent, so whether the user has a selection is
                    # unknown. Report the capture failure instead of an
                    # empty-selection shape.
                    logger.error(
                        "capture_selected_text: sentinel clipboard "
                        "write failed; selection state unknown."
                    )
                    return {
                        "text": "",
                        "flush_failed": False,
                        "select_all_fallback": False,
                        "copy_failed": False,
                        "capture_failed": True,
                    }

                # Try copying current selection.
                # wh-review-pattern-fixes.35: the copy must be
                # verified. press_keys discards the SendInput count,
                # and an undelivered Ctrl+C leaves the sentinel
                # unchanged -- indistinguishable from a genuine empty
                # selection. The false "no selection" would trigger
                # the Ctrl+A fallback below, and the AI flow would
                # capture and replace the ENTIRE field instead of the
                # user's selection. Fail closed: release a
                # possibly-held Ctrl (wh-eolas.2.5 shape) and report
                # copy_failed without entering the fallback.
                ok, accepted, expected = verified_press_keys('ctrl', 'c')
                if not ok:
                    _send_modifier_keyups(('ctrl',))
                    logger.error(
                        "capture_selected_text: Ctrl+C SendInput short "
                        "delivery (sent %d/%d); selection state "
                        "unknown, failing the capture closed.",
                        accepted, expected,
                    )
                    return {
                        "text": "",
                        "flush_failed": False,
                        "select_all_fallback": False,
                        "copy_failed": True,
                        "capture_failed": True,
                    }
                text = self._poll_clipboard(sentinel)

                if text is None:
                    # No selection detected -- select all and retry.
                    # wh-review-pattern-fixes.32 (d): the fallback flag
                    # drives a later caret-moving collapse on the Logic
                    # side, so it must reflect a Ctrl+A that actually
                    # landed. press_keys discards the SendInput count;
                    # an undelivered Ctrl+A would classify the user's
                    # own selection as a fallback selection and the
                    # collapse would move the user's caret. Send
                    # verified, and only arm the flag on full delivery.
                    ok, accepted, expected = verified_press_keys(
                        'ctrl', 'a'
                    )
                    if not ok:
                        # A short chord can leave Ctrl physically held
                        # (down accepted, up dropped) -- release it
                        # before reporting (wh-eolas.2.5 shape).
                        _send_modifier_keyups(('ctrl',))
                        logger.error(
                            "capture_selected_text: Ctrl+A SendInput "
                            "short delivery (sent %d/%d); not arming "
                            "select_all_fallback.",
                            accepted, expected,
                        )
                        return {
                            "text": "",
                            "flush_failed": False,
                            "select_all_fallback": False,
                            "copy_failed": False,
                            # wh-review-pattern-fixes.38: the field was
                            # never selected, so nothing could be
                            # captured -- this is a failure, not an
                            # empty selection.
                            "capture_failed": True,
                        }
                    # wh-review-pattern-fixes.28: record that Ctrl+A
                    # fired. From this point every exit -- including
                    # the failed second sentinel write below -- must
                    # report the armed whole-field selection.
                    select_all_fallback = True
                    time.sleep(0.02)
                    if not self.clipboard._safe_copy(sentinel):
                        # wh-review-pattern-fixes.38: the Ctrl+A landed
                        # and the sentinel write did not, so the whole
                        # field is selected and nothing was captured.
                        # Report both: the caller owes the collapse AND
                        # must not speak the no-text message.
                        logger.error(
                            "capture_selected_text: fallback sentinel "
                            "clipboard write failed after a verified "
                            "Ctrl+A; nothing captured."
                        )
                        return {
                            "text": "",
                            "flush_failed": False,
                            "select_all_fallback": True,
                            "copy_failed": False,
                            "capture_failed": True,
                        }
                    # wh-review-pattern-fixes.35: the fallback copy is
                    # verified too. It runs after a VERIFIED Ctrl+A, so
                    # on a short delivery the armed whole-field
                    # selection must still be reported alongside
                    # copy_failed -- the caller owes the collapse.
                    ok, accepted, expected = verified_press_keys(
                        'ctrl', 'c'
                    )
                    if not ok:
                        _send_modifier_keyups(('ctrl',))
                        logger.error(
                            "capture_selected_text: fallback Ctrl+C "
                            "SendInput short delivery (sent %d/%d); "
                            "failing the capture closed.",
                            accepted, expected,
                        )
                        return {
                            "text": "",
                            "flush_failed": False,
                            "select_all_fallback": True,
                            "copy_failed": True,
                            "capture_failed": True,
                        }
                    text = self._poll_clipboard(sentinel)

                captured = text or ""
        except Exception as e:
            logger.error(f"Error in capture_selected_text: {e}", exc_info=True)
            # wh-review-pattern-fixes.38: the handler swallows the
            # exception and falls through to the shared return below,
            # which used to be byte-identical to a genuine empty
            # selection. select_all_fallback keeps whatever the run
            # established before the raise, so a Ctrl+A that already
            # landed is still collapsed by the caller.
            capture_failed = True

        return {
            "text": captured,
            "flush_failed": False,
            "select_all_fallback": select_all_fallback,
            "copy_failed": False,
            "capture_failed": capture_failed,
        }

    def _resolve_captured_selection_target(
        self,
        capture_token: Optional[str],
        target_hwnd: Optional[int],
    ) -> Optional[CapturedTarget]:
        """Resolve the token the Logic process handed back to a control.

        wh-review-pattern-fixes.45: returns the remembered
        ``CapturedTarget`` only when all four conditions hold:

        1. The caller supplied a token and an HWND.
        2. This process still holds a captured target.
        3. The stored token equals the supplied token.
        4. The stored HWND equals the supplied HWND.

        Condition 4 is what makes a WRONG identity fail, not only a
        missing one. Returns None otherwise, and the caller must send
        no keystroke. The slot is cleared on a successful resolve, so
        one capture authorizes exactly one replacement.
        """
        if not capture_token or not target_hwnd:
            logger.error(
                "replace_selected_text: the caller supplied no captured "
                "target (token=%r, hwnd=%r); refusing to paste.",
                capture_token, target_hwnd,
            )
            return None
        slot = self._captured_selection_target
        if slot is None or slot[0] != capture_token:
            logger.error(
                "replace_selected_text: capture token %r does not match "
                "the captured target this process holds; refusing to "
                "paste.", capture_token,
            )
            return None
        remembered = slot[1]
        if remembered.hwnd != target_hwnd:
            logger.error(
                "replace_selected_text: the caller reported target "
                "hwnd=%s but the capture recorded hwnd=%s; refusing to "
                "paste.", target_hwnd, remembered.hwnd,
            )
            return None
        self._captured_selection_target = None
        return remembered

    def replace_selected_text(
        self,
        text: str,
        target_hwnd: Optional[int] = None,
        capture_token: Optional[str] = None,
    ) -> dict:
        """Replace current selection (or all text) with provided text.

        Uses clipboard paste for reliability.

        wh-review-pattern-fixes.45: this method used to write the
        clipboard and send Ctrl+V to whatever window held the
        foreground. The AI correction flow captures the selection, waits
        for a model that takes real time, and only then calls this
        method, so the user can move to another window while the model
        runs and have that window's selection replaced. ``target_hwnd``
        and ``capture_token`` name the control the capture read from.
        Both must match what ``capture_selected_text`` recorded, and the
        captured target must still hold the foreground, before this
        method writes the clipboard or sends a key. On any failure it
        returns ``focus_drift`` True and sends nothing. It attempts no
        repair: the user's original text is untouched, which is the
        whole point of the refusal.

        wh-review-pattern-fixes.33: does NOT wrap the paste in
        ``clipboard_context``. The old fixed schedule (restore_delay=
        0.05 plus a 0.05 s sleep) synchronously restored the user's old
        clipboard ~100 ms after the key request, so a slow target that
        processed the queued Ctrl+V after that point pasted the OLD
        clipboard over the selection while this method reported
        success -- the same race already fixed for raw_insert_text
        (wh-fsov0 / wh-qoyk9). Restoration is instead delegated to the
        utterance-level ownership-checked deferred path: the corrected-
        text write is marked via ``mark_clipboard_dirty(write_seq=...)``
        and ``end_utterance`` schedules the ``PendingRestore``. Outside
        an active utterance the mark schedules nothing and the corrected
        text stays on the clipboard -- the documented raw_insert_text
        trade-off, and safer than racing the paste.

        wh-review-pattern-fixes.32 (a): the Ctrl+V is sent through
        ``verified_press_keys`` and success is reported only after
        SendInput accepted the whole chord. press_keys discards the
        count; an unverified success here made Logic set replaced=True
        and speak "Done" while the captured text was still selected on
        screen.

        Args:
            text: Replacement text to paste.
            target_hwnd: The root-normalized top-level window handle
                ``capture_selected_text`` reported for this capture.
            capture_token: The token ``capture_selected_text`` issued for
                this capture.

        Returns:
            Dict with "success" and "focus_drift" keys. "success" is
            True only after the captured target proved it holds the
            foreground, the clipboard write succeeded, AND the Ctrl+V
            chord was fully accepted. "focus_drift" is True only when
            the captured target could not be resolved or no longer holds
            the foreground; nothing was written and no key was sent, so
            the user's text is exactly as they left it.
        """
        # wh-review-pattern-fixes.45: prove the captured target BEFORE
        # the clipboard write. A refusal must leave no trace: no
        # clipboard change, no keystroke, no dirty mark.
        captured = self._resolve_captured_selection_target(
            capture_token, target_hwnd,
        )
        if captured is None:
            return {"success": False, "focus_drift": True}
        if not self.clipboard.prove_captured_target_is_foreground(
            self.window_manager,
            captured.hwnd,
            captured.control,
            caller="replace_selected_text",
        ):
            logger.error(
                "replace_selected_text: the captured target (hwnd=%s) "
                "no longer holds the foreground; refusing to paste the "
                "replacement.", captured.hwnd,
            )
            return {"success": False, "focus_drift": True}
        try:
            # wh-fz7j.4: route through _safe_copy for seq tracking.
            # wh-fz7j.5: bail out if the copy failed; otherwise Ctrl+V
            # would paste the pre-existing clipboard contents.
            if not self.clipboard._safe_copy(text):
                return {"success": False, "focus_drift": False}
            # Mark the write for the deferred-restore manager with the
            # post-write seq so the ownership check has the WheelHouse
            # baseline (same shape as raw_insert_text).
            self.utterance_manager.mark_clipboard_dirty(
                write_seq=self.clipboard.last_clipboard_write_seq
            )
            self.utterance_manager._last_paste_time = time.time()
            time.sleep(0.02)
            ok, accepted, expected = verified_press_keys('ctrl', 'v')
            if not ok:
                # A short chord can leave Ctrl physically held --
                # release it before reporting (wh-eolas.2.5 shape).
                _send_modifier_keyups(('ctrl',))
                logger.error(
                    "replace_selected_text: Ctrl+V SendInput short "
                    "delivery (sent %d/%d); reporting failure.",
                    accepted, expected,
                )
                return {"success": False, "focus_drift": False}
        except Exception as e:
            logger.error(f"Error in replace_selected_text: {e}", exc_info=True)
            return {"success": False, "focus_drift": False}
        finally:
            self.buffer_manager.invalidate()

        return {"success": True, "focus_drift": False}

    # -----------------------------------------------------------------
    # Mouse-grid pointer actions (wh-input-mouse-primitives).
    #
    # The Input process is the only place that touches the mouse. Logic's
    # grid state machine owns every decision (which monitor, which cell,
    # where the mark is) and sends one of these three commands with a
    # resolved physical-pixel point; the Input process synthesises the
    # input and reports back.
    #
    # All three are in ``_HANDLES_OWN_RESPONSE``, all three are
    # never-raise, and all three emit exactly one ``MouseActionResponse``
    # per request_id -- the same contract the click handlers keep, for the
    # same reason (wh-lla5d: a dropped or doubled response leaves the
    # Logic awaiter to time out or the demuxer to warn).
    #
    # None of them runs UI Automation verification or the occlusion
    # hit-test the by-name click path uses: the user picked the point
    # visually off a painted grid, so there is no invisible control to
    # protect against (the design doc's "Verification difference").
    # -----------------------------------------------------------------

    def _get_validated_click_config(self):
        """Return the validated ``[click]`` config, building it once.

        ``ClickConfig.from_raw`` never raises and degrades to a disabled
        config on a bad value. The grid lives behind the same master switch
        as voice clicking (design doc, "Configuration"), so a disabled
        ``[click]`` block disables the grid's pointer actions too. Logic
        gates before sending; this defends the Input side against a stale or
        racing request, exactly as ``_get_click_element_finder`` does for the
        by-name path.

        This deliberately does NOT build an ElementFinder or a COM
        automation root -- a pointer action needs neither.
        """
        cfg = getattr(self, "_click_config", None)
        if cfg is None:
            from ui.click_config import ClickConfig

            cfg = ClickConfig.from_raw(self.config.get("click", {}))
            self._click_config = cfg
        return cfg

    def _emit_mouse_action_response(
        self,
        *,
        action_name: str,
        request_id: Optional[str],
        trace_id: str,
        succeeded: bool,
        reason: Optional[str],
    ) -> None:
        """Put exactly one ``MouseActionResponse`` on the response queue.

        A dead response queue is the ONE case that yields zero responses:
        there is no second channel to deliver on, and the orphaned Logic
        Future is already covered by Logic's own timeout.
        """
        from services.wheelhouse.shared.mouse_action import MouseActionResponse

        payload = MouseActionResponse(
            status="ok" if succeeded else "error",
            outcome="ok" if succeeded else "execution_failed",
            reason=None if succeeded else (reason or "unexpected_error"),
            trace_id=trace_id if isinstance(trace_id, str) else "",
        ).to_dict()
        # Record the result BEFORE the enqueue attempt (wh-mouse-grid.1.24):
        # when the reply reaches Logic after its timeout, the app demuxer
        # discards it, so channel_probe's reply carries this record and Logic
        # correlates it by trace_id to recover a release_failed warning. A
        # failed enqueue is exactly the case where recovery matters, so the
        # record must not depend on the put succeeding. This command loop is
        # single-threaded, so the read in channel_probe cannot race this
        # write.
        self._last_mouse_action_result = {
            "action": action_name,
            "succeeded": bool(succeeded),
            "reason": payload["reason"],
            "trace_id": payload["trace_id"],
        }
        if request_id is not None:
            payload["request_id"] = request_id
        payload["action"] = action_name
        try:
            self.response_queue.put(payload)
        except Exception as exc:  # noqa: BLE001 -- log and drop
            logger.error(
                "%s: failed to enqueue response (trace_id=%s): %s",
                action_name, trace_id, exc,
            )

    def channel_probe(
        self,
        trace_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Channel drain probe (wh-mouse-grid.1.18, extended by .1.24).

        Logic sends this after a grid pointer request times out. Because this
        command loop executes one command at a time in arrival order, ANY
        reply proves every command queued before the probe has already
        executed or been overwritten -- so the timed-out action can no longer
        fire later.

        The reply also carries ``last_mouse_action`` -- the recorded result
        of the most recent pointer action (or ``None``) -- because the
        timed-out action's own reply was discarded by the app demuxer after
        Logic's timeout. Logic correlates the record by trace_id to recover
        the release_failed stuck-button warning (wh-mouse-grid.1.24). The
        in-order command loop guarantees the timed-out action's record was
        written before this probe runs. In ``_HANDLES_OWN_RESPONSE`` so the
        generic emitter does not clobber the record-bearing reply; touches
        no input hardware.
        """
        payload: dict = {
            "status": "ok",
            "trace_id": trace_id if isinstance(trace_id, str) else "",
            "last_mouse_action": getattr(
                self, "_last_mouse_action_result", None
            ),
            "action": "channel_probe",
        }
        if request_id is not None:
            payload["request_id"] = request_id
        try:
            self.response_queue.put(payload)
        except Exception as exc:  # noqa: BLE001 -- log and drop
            logger.error(
                "channel_probe: failed to enqueue response (trace_id=%s): %s",
                trace_id, exc,
            )

    def click_point(
        self,
        x=None,
        y=None,
        button: str = "left",
        click_count: int = 1,
        trace_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Click at a physical-pixel screen point (mouse-grid gesture).

        Args:
            x, y: physical screen pixels. Negative values are normal on a
                multi-monitor desktop whose secondary monitor sits left of or
                above the primary.
            button: ``"left"`` or ``"right"``.
            click_count: 1 or 2 (a double click).
            trace_id: Logic-generated correlation id, echoed in the response.

        Emits one ``MouseActionResponse``. The argument validation lives in
        the SendInput primitive, which reports ``invalid_point`` /
        ``invalid_button`` / ``invalid_click_count`` rather than raising, so a
        malformed IPC message produces a normal ``execution_failed`` response.
        """
        action_name = "click_point"
        try:
            if not self._get_validated_click_config().grid_enabled_effective:
                logger.info(
                    "click_point: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            # wh-review-pattern-fixes.8: a grid click can move focus, the
            # caret, or the selection; invalidate before the dispatch (see
            # the hotkey_action block comment for the full rationale).
            self.buffer_manager.invalidate()
            succeeded, reason = self._mouse_click_seam(
                x, y, button, click_count,
            )
            logger.info(
                "click_point: point=(%r,%r) button=%s count=%r ok=%s "
                "reason=%s (trace_id=%s)",
                x, y, button, click_count, succeeded, reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "click_point: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )

    def move_pointer(
        self,
        x=None,
        y=None,
        trace_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Park the pointer at a physical-pixel point, pressing nothing.

        The grid's "move here" gesture. Hover-revealed menus are common in
        exactly the applications with poor accessibility trees that motivate
        the grid, so parking the pointer is a first-class action rather than a
        side effect of clicking.

        Emits one ``MouseActionResponse``.
        """
        action_name = "move_pointer"
        try:
            if not self._get_validated_click_config().grid_enabled_effective:
                logger.info(
                    "move_pointer: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            succeeded, reason = self._mouse_move_seam(x, y)
            logger.info(
                "move_pointer: point=(%r,%r) ok=%s reason=%s (trace_id=%s)",
                x, y, succeeded, reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "move_pointer: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )

    def perform_drag(
        self,
        start_x=None,
        start_y=None,
        end_x=None,
        end_y=None,
        duration_ms: int = 250,
        trace_id: str = "",
        request_id: Optional[str] = None,
        **kwargs,
    ) -> None:
        """Drag from one physical-pixel point to another as ONE operation.

        The whole gesture -- button down at the start, interpolated movement
        across ``duration_ms``, button up at the end -- runs inside the
        SendInput primitive, so no IPC boundary sits between the press and the
        release and no message loss can strand the button pressed. The
        primitive guarantees the release with a try/finally even when the
        movement fails partway.

        The movement is gradual because many applications ignore a drag whose
        pointer teleports: they never see the intermediate movement that makes
        them begin the drag operation.

        Emits one ``MouseActionResponse``. The ``release_failed`` reason is
        the one outcome meaning a mouse button may still be held; it reaches
        Logic verbatim so the notice can say so.
        """
        action_name = "perform_drag"
        try:
            if not self._get_validated_click_config().grid_enabled_effective:
                logger.info(
                    "perform_drag: voice clicking disabled by config; "
                    "short-circuiting (trace_id=%s)", trace_id,
                )
                self._emit_mouse_action_response(
                    action_name=action_name, request_id=request_id,
                    trace_id=trace_id, succeeded=False,
                    reason="disabled_by_config",
                )
                return

            # wh-review-pattern-fixes.8: a drag can move focus, the caret,
            # or the selection; invalidate before the dispatch (see the
            # hotkey_action block comment for the full rationale).
            self.buffer_manager.invalidate()
            succeeded, reason = self._mouse_drag_seam(
                start_x, start_y, end_x, end_y, duration_ms,
            )
            logger.info(
                "perform_drag: (%r,%r)->(%r,%r) duration_ms=%r ok=%s "
                "reason=%s (trace_id=%s)",
                start_x, start_y, end_x, end_y, duration_ms, succeeded,
                reason, trace_id,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=bool(succeeded), reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 -- never-raise handler
            logger.error(
                "perform_drag: unexpected error (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._emit_mouse_action_response(
                action_name=action_name, request_id=request_id,
                trace_id=trace_id, succeeded=False,
                reason="unexpected_error",
            )

    def _poll_clipboard(self, sentinel: str) -> str | None:
        """Poll clipboard until content differs from sentinel, or timeout.

        Args:
            sentinel: The sentinel value placed on clipboard before copy.

        Returns:
            Clipboard text if changed from sentinel, None if timeout.
        """
        timeout = self.clipboard.clipboard_verification_timeout
        start = time.time()
        while True:
            time.sleep(0.005)
            current = pyperclip.paste()
            if current != sentinel:
                return current
            if time.time() - start > timeout:
                return None


