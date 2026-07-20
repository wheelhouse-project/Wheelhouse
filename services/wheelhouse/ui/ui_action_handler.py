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
import pyperclip
import win32gui
import win32con
import win32process
from multiprocessing import Queue
from typing import Optional
from utils.win_input_sender import press_keys, type_string, send_backspaces
from utils.clipboard_manager import clipboard_context
from utils.redact import redact_transcript

from .text_perfector import TextPerfector
from .clipboard_operations import ClipboardOperations
from .window_focus_manager import WindowFocusManager
from .selection_transformer import SelectionTransformer
from .utterance_clipboard_manager import UtteranceClipboardManager
from .shadow_buffer import ShadowBufferManager
from .terminal_editor_proxy import TerminalEditorProxy
from .response_handler import ResponseHandler
from .hwnd_utils import (
    normalize_hwnd_for_foreground_compare,
    resolve_same_process_browser_names,
)

# New Router/Strategy Components
from .context import capture_context
from .elevation_check import target_elevation_state
from .router import InsertionRouter
from .strategies.base import InsertionMode, InsertionOptions
from .strategies.specific import (
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


# Keys that invalidate shadow buffer cache
CACHE_INVALIDATING_KEYS = {
    'backspace', 'enter', 'delete', 'tab', 'left', 'right', 'up', 'down',
    'home', 'end', 'pageup', 'pagedown'
}


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


def _win32_coordinate_click(x: int, y: int) -> tuple[bool, int]:
    """SendInput-backed coordinate click for the executor's fallback seam.

    Wires the real :func:`utils.win_input_sender.click_at` into
    ``ClickExecutor`` (wh-l4h.1). ``click_at`` is itself fail-closed (it
    verifies the cursor landed before synthesising any button event and
    returns ``(False, 0)`` on a wrong landing) and fail-soft (any internal
    Win32/ctypes error returns ``(False, 0)``). This wrapper adds defence in
    depth so an import or call error at the seam boundary cannot escape into
    the click path -- it mirrors the sibling ``_win32_*`` seams. The executor
    also catches a raising seam, so this is belt-and-braces.

    Returns ``(succeeded, events_sent)`` where ``events_sent`` counts only the
    LEFTDOWN/LEFTUP pair, so the executor's ``events_sent < 2`` short-send
    check stays meaningful.
    """
    try:
        from utils.win_input_sender import click_at

        return click_at(int(x), int(y))
    except Exception:  # noqa: BLE001 -- fail soft, never propagate
        return (False, 0)


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

    def __init__(self, response_queue: Queue, config: dict):
        """Initialize UI action handler with all specialist components.

        Args:
            response_queue: Queue for sending action responses
            config: Configuration dictionary (not ConfigService - can't pickle)
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

        # Initialize all specialist components
        self.text_perfector = TextPerfector()
        self.clipboard = ClipboardOperations(config)
        self.window_manager = WindowFocusManager()
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
        # __init__ from the same config dict).
        same_process_names = resolve_same_process_browser_names(config)
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

        # Retraction state (reset per utterance)
        self._user_interacted_during_utterance: bool = False
        self._used_simple_paste: bool = False

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
        # Flush any buffered letters with compression before ending utterance
        self._flush_letter_buffer()

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
        1. User interaction during utterance -> block (user_interacted)
        2. SimplePaste strategy was used -> block (simple_paste)
        3. Optimistic paste under clipboard contention -> block
           (paste_unverified).
        4. Remembered target HWND no longer foreground -> block
           (focus_drifted).
        5. Nothing pasted and no buffered letters -> block
           (nothing_to_retract).
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
        if self._user_interacted_during_utterance:
            logger.info("Retraction blocked: user interacted during utterance")
            return {"status": "not_retracted", "reason": "user_interacted"}

        if self._used_simple_paste:
            logger.info("Retraction blocked: SimplePaste strategy was used")
            return {"status": "not_retracted", "reason": "simple_paste"}

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
        self.buffer_manager.invalidate()
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

    def _flush_letter_buffer(self):
        """Flush buffered letters with auto-compression applied.
        
        If buffer has 3+ letters, compresses them to a single word.
        Otherwise, outputs letters as-is separated by spaces.
        """
        if not self._letter_buffer:
            return
        
        # Join letters with spaces and apply compression
        buffered_text = " ".join(self._letter_buffer)
        compressed_text = auto_compress_spelled_letters(buffered_text)
        
        logger.info(f"[LETTER_BUFFER] Flushing: '{redact_transcript(buffered_text)}' -> '{redact_transcript(compressed_text)}'")
        
        # Clear buffer before inserting to prevent recursion
        self._letter_buffer.clear()
        
        # Insert the compressed text directly (bypass buffering logic)
        self._do_direct_insert(compressed_text, request_id=None)

    def _do_direct_insert(self, text: str, request_id: Optional[str] = None):
        """Insert text directly without letter buffering."""
        if self.utterance_manager.is_in_utterance():
            self._execute_insert_with_ack(text, request_id)
        else:
            with clipboard_context(restore_delay=0.05):
                self._execute_insert_with_ack(text, request_id)

    # ========================================================================
    # PUBLIC API - Main Text Insertion
    # ========================================================================

    def intelligent_insert_text(
        self,
        insertion_string: str,
        request_id: Optional[str] = None,
        target_hwnd: Optional[int] = None,
        **kwargs,
    ):
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
        """
        # LETTER BUFFERING: Buffer single letters for auto-compression
        # When we see a non-single-letter word, flush buffer first
        if self._is_single_letter(insertion_string):
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
            return
        else:
            # Non-single-letter word: flush any buffered letters first
            if self._letter_buffer:
                self._flush_letter_buffer()

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
                return

        # wh-4z4g9: dirty tracking moved to _execute_insert_with_ack so it
        # only fires when the chosen strategy actually wrote the system
        # clipboard. _last_paste_time is also moved there so the
        # wrap_or_insert "recent paste" heuristic does not falsely fire on
        # a Unicode-only utterance that never touched the clipboard.

        if self.utterance_manager.is_in_utterance():
            self._execute_insert_with_ack(insertion_string, request_id)
        else:
            with clipboard_context(restore_delay=0.05):
                self._execute_insert_with_ack(insertion_string, request_id)

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
        """
        try:
            # Capture Context
            context = capture_context()

            # Remember window handle for focus restoration
            if context.focused_control:
                self.window_manager.remember_target(context.focused_control)

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

            # Track strategy type for retraction gating
            if isinstance(strategy, SimplePasteStrategy):
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
            return result.success

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

        Returns the strategy's success bool. When ``request_id`` is not
        None, also emits a Schema A response so the caller's Future
        resolves; pass ``None`` to suppress emission when the caller
        owns its own response.
        """
        return self._execute_insert_with_ack(
            text,
            request_id,
            options=InsertionOptions(mode=InsertionMode.VERBATIM),
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
            focused_control = context.focused_control
            is_flutter = context.is_flutter
            
            with clipboard_context(restore_delay=0.05):
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
                    press_keys('ctrl', 'c')

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
                success = self.verbatim_insert_text(
                    transformed_text, request_id=None,
                )

                if success:
                    message = f"Successfully transformed selection with {transformation_type}"
                    logger.info(message)
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

                    # Save original clipboard
                    original_clipboard = pyperclip.paste()

                    # Send Ctrl+C to copy any selection
                    if is_flutter and focused_control and focused_control.Exists(0, 0):
                        focused_control.SendKeys('{Ctrl}c')
                    else:
                        press_keys('ctrl', 'c')
                    
                    # Poll clipboard to see if it changed
                    start_time = time.time()
                    timeout = self.clipboard.clipboard_verification_timeout
                    current_clipboard = original_clipboard
                    poll_count = 0
                    
                    while current_clipboard == original_clipboard:
                        time.sleep(0.005)  # 5ms polling interval
                        current_clipboard = pyperclip.paste()
                        poll_count += 1
                        if time.time() - start_time > timeout:
                            break  # Timeout - no selection detected
                    
                    if current_clipboard != original_clipboard:
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
                        logger.info(f"Wrapping selected text: '{redact_transcript(current_clipboard[:50])}'...")
                        wrapped = f"{left_fence}{current_clipboard}{right_fence}"
                        self.verbatim_insert_text(wrapped, request_id)
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
                self.intelligent_insert_text(empty_delimiters, request_id=None)

                # Move cursor left to position between delimiters
                time.sleep(0.05)  # Small delay to ensure text is inserted
                self.press_key_action("left", repeat=1)

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
        if isinstance(strategy, SimplePasteStrategy):
            self._used_simple_paste = True
        elif isinstance(strategy, ClipboardOnlyStrategy):
            # wh-9weum Phase 1 (wh-0ci9n): same rationale as the
            # _execute_insert_with_ack branch -- soft-fallback paste
            # poisons retract because the paste's actual landing in
            # the target cannot be confirmed.
            self._used_simple_paste = True
            # Review wh-kox5.3: invalidate the shadow buffer; see the
            # _execute_insert_with_ack branch for the full reasoning.
            self.buffer_manager.invalidate()

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
        try:
            type_string(text)
        except Exception as e:
            logger.error(f"Error in type_text: {e}", exc_info=True)

    def terminal_editor_cancelled(self):
        """Handle editor cancellation from GUI Process."""
        self.terminal_editor.force_cleanup()

    def start_overlay_walk(
        self,
        scope: str = "focused_window",
        overlay_session_id: int = 0,
        paint_generation: int = 0,
        trace_id: str = "",
        request_id: Optional[str] = None,
        command_dequeue_monotonic: Optional[float] = None,
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
            # this handler's entry instant. walk_deadline_ms comes from the
            # validated _click_config (set as a side effect of the finder build);
            # None when no walk bound is configured (defensive -- finder is
            # non-None here). overlay_walk already accepts deadline= and threads
            # it into walk_window unchanged.
            dequeue_monotonic = (
                command_dequeue_monotonic
                if command_dequeue_monotonic is not None
                else time.monotonic()
            )
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
                "start_overlay_walk: walking focused window in process=%s "
                "(session=%s gen=%s trace_id=%s)",
                foreground.foreground_process_name,
                overlay_session_id, paint_generation, trace_id,
            )
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
            if walk.outcome == "no_targets":
                logger.info(
                    "start_overlay_walk: no_targets (trace_id=%s)", trace_id,
                )
                _emit(StartOverlayWalkResponse(
                    status="ok",
                    outcome="no_targets",
                    reason=None,
                    snapshot_id=snapshot_id,
                    snapshot_summary=walk.summary,
                    trace_id=trace_id,
                    overlay_session_id=overlay_session_id,
                    paint_generation=paint_generation,
                ))
                return

            # outcome == "ok".
            item_count = (
                len(walk.summary.items) if walk.summary is not None else 0
            )
            logger.info(
                "start_overlay_walk: ok with %d items (snapshot=%s trace_id=%s)",
                item_count, snapshot_id, trace_id,
            )
            _emit(StartOverlayWalkResponse(
                status="ok",
                outcome="ok",
                reason=None,
                snapshot_id=snapshot_id,
                snapshot_summary=walk.summary,
                trace_id=trace_id,
                overlay_session_id=overlay_session_id,
                paint_generation=paint_generation,
            ))
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
        """Slide the Input store's TTL for the still-visible pinned snapshot.

        The Input side of the Logic-side 15-second overlay keepalive
        (wh-overlay-snapshot-keepalive). Logic sends this every keepalive tick
        for the snapshot the overlay is currently showing; the handler slides
        that snapshot's TTL anchor via
        :meth:`ElementFinder.refresh_snapshot_ttl` so a numbered overlay left on
        screen past the TTL stays clickable. Without it the Logic resolver cache
        and the Input store expire independently and "click N" fails with
        ``snapshot_expired`` on a still-visible overlay.

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
        **kwargs,
    ) -> None:
        """Click a numbered-overlay item by item_id (wh-tab7j / wh-jfavj).

        Phase 1.5 of the voice-element-clicking feature (epic wh-l4h.1). When
        the user clicks a numbered overlay badge, Logic resolves the display
        number to an ``item_id`` from its retained ``WalkSnapshotSummary`` and
        forwards this request carrying ``snapshot_id`` + ``item_id``
        (+ ``trace_id`` + ``request_id``). The Input process owns the click:

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
            # coordinate-eligibility check (_coord_eligible).
            query = ElementQuery(
                name=match.name,
                role=match.role,
                ordinal=None,
                spatial=None,
                raw_utterance="",
            )
            snap_fg = SnapshotForeground(
                window=foreground.foreground_window,
                pid=foreground.foreground_pid,
                process_name=foreground.foreground_process_name,
                window_creation_time=foreground.foreground_window_creation_time,
            )
            executor = self._get_click_executor()
            click_result = executor.click(match, snap_fg, query)
            if click_result.outcome == "ok":
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
            # Real Win32 popup-closed probe seams (wh-n29v.71). Without these
            # ClickExecutor._popup_still_open fails closed (returns False when
            # either probe is None) and EVERY popup-owned winner is refused
            # execution_failed:popup_closed. _default_is_window_visible wraps
            # IsWindowVisible; _default_owner_of wraps GetWindow(GW_OWNER).
            # These are pure Win32 (no COM apartment affinity), so they are the
            # module-level callables, not bound to the IUIAutomation root.
            popup_visible_fn=uia_walker._default_is_window_visible,
            popup_owner_fn=uia_walker._default_owner_of,
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
            # top-level), and we skip the refocus call in that case
            # so the win32 layer is not touched with a zero handle.
            target_hwnd = result.target_hwnd
            target_pid = result.target_process_id
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

            context = capture_context()
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
                )
            else:
                with clipboard_context(restore_delay=0.05):
                    insertion_result = self.clipboard_only_strategy.insert(
                        cached_text, context, request_id, None,
                    )

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
        """Press a key, invalidating buffer if necessary.

        Args:
            key: Key to press
            repeat: Number of times to repeat
        """
        if 'request_id' in kwargs:
            logger.warning(f"Unexpected 'request_id' in press_key_action for key '{key}'")

        if key.lower() in CACHE_INVALIDATING_KEYS:
            self.buffer_manager.invalidate()

        try:
            if key.lower() == "enter" and self.terminal_editor.is_active:
                self.terminal_editor.submit()
                return

            # Capture Context for Flutter detection
            context = capture_context()
            is_flutter = context.is_flutter
            focused_control = context.focused_control
            
            # Flutter apps filter SendInput for special keys, so use press_keys instead
            # SendKeys doesn't support special keys (only text input)
            if is_flutter:
                logger.debug(f"[FLUTTER] press_key_action: key='{key}' - using press_keys (SendInput) for special key")
            
            for _ in range(repeat):
                press_keys(key)
        except Exception as e:
            logger.error(f"Error pressing key '{key}': {e}", exc_info=True)

    def hotkey_action(self, keys: list, repeat: int = 1, **kwargs):
        """Execute a hotkey combination, optionally repeated.

        Args:
            keys: List of keys to press together
            repeat: Number of times to repeat the hotkey (default: 1)
        """
        if 'request_id' in kwargs:
            logger.warning(f"Unexpected 'request_id' in hotkey_action for keys '{keys}'")

        try:
            normalized_keys = [str(k).lower() for k in keys]
            if self.terminal_editor.is_active and normalized_keys == ["enter"]:
                self.terminal_editor.submit()
                return

            # Capture Context for Flutter detection
            context = capture_context()
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
                    press_keys(*keys)
        except Exception as e:
            logger.error(f"Error executing hotkey '{keys}' (repeat={repeat}): {e}", exc_info=True)
    
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
            from plyer import notification

            # Validate parameters
            if not isinstance(title, str) or not isinstance(message, str) or not isinstance(timeout, int):
                logger.error(
                    f"Invalid parameter types: title={type(title)}, "
                    f"message={type(message)}, timeout={type(timeout)}"
                )
                return

            try:
                if hasattr(notification, 'notify') and callable(notification.notify):
                    notification.notify(
                        title=title,
                        message=message,
                        app_name='Wheelhouse',
                        timeout=timeout
                    )
            except Exception as e:
                logger.error(f"Notification dispatch failed: {e}")
        except Exception as e:
            logger.error(f"Failed to show notification: {e}", exc_info=True)
    # ========================================================================
    # PUBLIC API - AI Clipboard Operations
    # ========================================================================

    def capture_selected_text(self) -> dict:
        """Capture selected text via clipboard for AI text correction.

        Uses the sentinel clipboard pattern (proven in transform_selection):
        1. Set sentinel on clipboard
        2. Send Ctrl+C to copy selection
        3. Poll clipboard until it changes from sentinel
        4. If unchanged (no selection), send Ctrl+A then Ctrl+C to select all
        5. Return captured text

        Clipboard is saved and restored via clipboard_context.

        Returns:
            Dict with "text" key containing captured text (empty string if none).
        """
        captured = ""
        try:
            with clipboard_context(restore_delay=0.05):
                sentinel = f"__SENTINEL__{time.time()}"
                # wh-fz7j.4: route through _safe_copy for seq tracking.
                # wh-fz7j.5: bail out if the sentinel write fails; otherwise
                # _poll_clipboard would compare against a sentinel that was
                # never copied and could return stale clipboard text.
                if not self.clipboard._safe_copy(sentinel):
                    return {"text": ""}

                # Try copying current selection
                press_keys('ctrl', 'c')
                text = self._poll_clipboard(sentinel)

                if text is None:
                    # No selection detected -- select all and retry
                    press_keys('ctrl', 'a')
                    time.sleep(0.02)
                    if not self.clipboard._safe_copy(sentinel):
                        return {"text": ""}
                    press_keys('ctrl', 'c')
                    text = self._poll_clipboard(sentinel)

                captured = text or ""
        except Exception as e:
            logger.error(f"Error in capture_selected_text: {e}", exc_info=True)

        return {"text": captured}

    def replace_selected_text(self, text: str) -> dict:
        """Replace current selection (or all text) with provided text.

        Uses clipboard paste for reliability. Clipboard is saved and
        restored via clipboard_context.

        Args:
            text: Replacement text to paste.

        Returns:
            Dict with "success" key.
        """
        try:
            with clipboard_context(restore_delay=0.05):
                # wh-fz7j.4: route through _safe_copy for seq tracking.
                # wh-fz7j.5: bail out if the copy failed; otherwise Ctrl+V
                # would paste the pre-existing clipboard contents.
                if not self.clipboard._safe_copy(text):
                    return {"success": False}
                time.sleep(0.02)
                press_keys('ctrl', 'v')
                time.sleep(0.05)
        except Exception as e:
            logger.error(f"Error in replace_selected_text: {e}", exc_info=True)
            return {"success": False}
        finally:
            self.buffer_manager.invalidate()

        return {"success": True}

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


