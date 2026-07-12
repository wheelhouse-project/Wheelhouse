"""
This module serves as the central hub for the application's logic process.

It defines the `LogicController`, which orchestrates all major functionalities by coordinating
several key components:

- **ServiceManager**: Responsible for the lifecycle of "Services". It creates, holds, and
  shuts down all the long-running service objects (e.g., `MouseHandler`, `BraviaControl`).

- **StateManager**: Manages the application's state (e.g., if speech is enabled, if the
  window mover is active) and acts as the single source of truth. It synchronizes state
  with the GUI process and handles configuration changes.

- **Services**: These are the individual components (`MouseHandler`, `AudioMonitor`, etc.) that
  encapsulate a major piece of functionality. They are the "actors" that hold state and logic.

- **Tasks**: An `asyncio.Task` representing a running background job (e.g., listening for
  mouse events). The `LogicController` creates tasks by calling methods on Services. They
  are the "actions" performed by the services.

The `LogicController` launches and monitors these tasks, ensuring that if any essential
task fails, the entire application shuts down gracefully. The module also contains the
entry point for the logic process (`start_logic_process`).

### Plugin Architecture Vision

This application is designed with a future plugin system in mind. The core
architectural decisions, such as the `ConfigService` and `EventBus`, support
this vision:

1.  **Plugin Management:** The main `config.toml` will be used to enable or
    disable plugins.
2.  **Plugin Configuration:** Each plugin will have its own `config.toml` file
    for its specific settings.
3.  **Dependency Injection:** The application's composition root (this module)
    will be responsible for instantiating a `PluginManager`. This manager will
    inject two `ConfigService` instances into each plugin: one for the main
    application config and one for the plugin's own config, ensuring clear
    separation of configuration.
"""

import asyncio
import enum
import logging
import os
import queue as queue_module
import signal
from multiprocessing import Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from services.wheelhouse.click_first_use_hint import FirstUseHintTracker
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer


class AddSoftAllowOutcome(enum.Enum):
    """Outcome of LogicController.add_soft_allow (wh-vbvgf.9.2 codex review).

    Distinguishes the three cases so the Yes-path handler can apply
    the right post-grant policy. SUCCESS and IPC_FAILED both leave a
    durable grant on disk; DISK_FAILED does not.
    """

    SUCCESS = "success"
    IPC_FAILED = "ipc_failed"
    DISK_FAILED = "disk_failed"

    @property
    def is_durable(self) -> bool:
        """True when the soft-allow tuple is on disk (will load on next run)."""
        return self in (AddSoftAllowOutcome.SUCCESS, AddSoftAllowOutcome.IPC_FAILED)


# wh-grant-ipc-failed-ux: backoff between add_soft_allow_tuple IPC retries
# after a successful disk write. One initial send plus one retry per entry.
# Tests override per-instance via _soft_allow_ipc_retry_delays.
_SOFT_ALLOW_IPC_RETRY_DELAYS = (0.2, 0.5)

from services.wheelhouse.app import WheelHouseApp
from services.wheelhouse.click_counter import ClickCounter
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.events import RetryThresholdReached, RetryVerified
from services.wheelhouse.service_manager import ServiceManager
from services.wheelhouse.shared.consumed_token_set import ConsumedTokenSet
from services.wheelhouse.shared.rejection_token_cache import (
    RejectionTokenCache,
    RejectionTuple,
)
from services.wheelhouse.state_manager import StateManager
from services.wheelhouse.integrations.websocket_manager import WebSocketManager
from services.wheelhouse.utils.logging_setup import setup_logging
from services.wheelhouse.utils.screen_reader_flag import (
    apply_screen_reader_flag,
    clear_screen_reader_flag,
)
from services.wheelhouse.utils.soft_allow_writer import append_soft_allow_tuple
from services.wheelhouse.utils.declined_writer import append_declined_tuple
from services.wheelhouse.utils.system import get_app_data_path
from services.wheelhouse.config_service import ConfigService

# Import version info from parent services directory
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from version_info import get_startup_banner


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a Z suffix.

    Used for the ``added_at`` field in soft-allow entries. The Z suffix
    matches the documented schema and keeps the file portable across
    locales.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _screen_reader_flag_intent(click_cfg) -> bool:
    """Return whether the screen-reader flag should be SET at startup (wh-69sk8).

    True only when voice clicking is enabled AND the user opted in
    (enable_screen_reader_flag); every other case CLEARS the flag.
    """
    return bool(click_cfg.enabled and click_cfg.enable_screen_reader_flag)


# Win32 WinEvent constants for the overlay focus hooks (wh-n29v.21). Mirrors
# features/window_mover.py's values; defined here so the hook manager below has
# no hard dependency on importing that module.
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_DESTROY = 0x8003
_OBJID_WINDOW = 0  # only top-level-window destroys (OBJID_WINDOW) are of interest

# wh-overlay-stale-click-refresh: the pre-click verification reasons that mean
# the numbered badge no longer points at a clickable control IN THE SAME PLACE,
# so a fresh walk + repaint of the overlay is the right response to a refused
# "click N". ClickExecutor._verify (ui/click_executor.py) returns these:
#   bounds_invalid        -- the live position is unreadable or zero-area now
#   bounds_stale          -- the control drifted past the per-dimension tolerance
#   target_moved_offscreen-- the control's centre scrolled off-screen (this check
#                            runs BEFORE the bounds_stale drift check, so a fully
#                            scrolled-away control reports THIS, not bounds_stale)
#   popup_closed          -- the owning popup the badge pointed at is gone
# Excluded on purpose: "disabled", an Invoke/COM failure, and "item_not_found"
# are NOT position staleness -- a re-walk finds the same control in the same
# place (or the same missing item), so it would not change the outcome and would
# only churn the overlay.
_OVERLAY_REWALK_REFUSAL_REASONS = frozenset(
    {"bounds_invalid", "bounds_stale", "target_moved_offscreen", "popup_closed"}
)

# wh-overlay-fixqueue-review.2: how long after a PROACTIVE refresh swap a
# "click N" is checked against the pre-swap badge identities. Sized to cover
# the read-then-speak-then-STT round trip of an utterance begun against the
# old badges; a click blocked by the check shows the "numbers just changed"
# notice instead. Module-level (not a class attribute) so the real value is
# read even on MagicMock(spec=LogicController) test controllers.
_OVERLAY_RENUMBER_GRACE_SECONDS = 3.0


class OverlayFocusHookManager:
    """Thin Win32 ``SetWinEventHook`` seam for the numbered overlay (wh-n29v.21).

    This is the DELIBERATELY-THIN, hard-to-unit-test seam that mirrors the
    registration shape in ``features/window_mover.py``: a message-only window
    on a dedicated daemon thread, a ``WinEventProcType`` callback, and a
    ``PeekMessage`` pump. All the testable DECISION logic (debounce,
    generation supersession, identity comparison, raw-event -> OverlayEvent
    mapping) lives in the pure module ``overlay_focus_hooks.py``; this class
    only registers the OS hooks and marshals each callback onto the owning
    asyncio loop via ``loop.call_soon_threadsafe`` before any state is touched.

    Three hooks:

      * A FOREGROUND hook (``EVENT_SYSTEM_FOREGROUND``,
        ``WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS``) registered for the
        whole lifetime of the manager. Its callback forwards the new HWND to
        ``on_foreground(hwnd)`` on the loop.
      * A MENU POP-UP hook (the ``EVENT_SYSTEM_MENUPOPUPSTART`` ..
        ``MENUPOPUPEND`` range, same flags) registered alongside the
        foreground hook when ``on_menu_popup`` is supplied
        (wh-overlay-menu-close-stale). Closing a menu does not change the
        foreground window, so without this hook badges painted for the (now
        gone) menu items stay floating over the page. Its callback forwards
        the raw event id to ``on_menu_popup(event_id)`` on the loop.
        Registration failure is NON-fatal (a warning; focus following still
        works) -- the menu re-walk is an improvement, not the manager's
        purpose.
      * A TRANSIENT DESTROY hook (``EVENT_OBJECT_DESTROY``,
        ``WINEVENT_OUTOFCONTEXT``) registered only while the overlay is
        ``paused``, scoped to the tracked window's process/thread id via the
        ``idProcess`` / ``idThread`` parameters of ``SetWinEventHook``. Its
        callback forwards ``(destroyed_hwnd, object_id)`` to
        ``on_destroy(destroyed_hwnd)`` on the loop. ``register_destroy_hook`` /
        ``unregister_destroy_hook`` are called from the Logic loop when the
        machine enters / leaves ``paused``.

    The thread is started by :meth:`start` and torn down by :meth:`stop`, which
    unhooks both hooks (no leaked hooks on shutdown). The callbacks NEVER touch
    the state machine directly -- they only schedule the loop callbacks -- so
    the machine stays single-thread-owned by the Logic loop.
    """

    def __init__(
        self, loop, on_foreground, on_destroy, on_menu_popup=None,
    ) -> None:
        self._loop = loop
        self._on_foreground = on_foreground
        self._on_destroy = on_destroy
        # wh-overlay-menu-close-stale: optional; when None the menu pop-up
        # hook is not registered (older construction sites keep working).
        self._on_menu_popup = on_menu_popup

        self._running = False
        self._thread = None
        self._message_only_hwnd = None
        self._window_class_name = f"OverlayFocusHookMsgWnd_{os.getpid()}"
        self._thread_ready = None  # threading.Event, created in start()

        self._foreground_proc_cb = None
        self._foreground_hook = None
        self._menu_popup_proc_cb = None
        self._menu_popup_hook = None
        # Transient destroy hook bookkeeping; mutated only from the hook thread
        # via the WM_USER request below or, during teardown, from stop().
        self._destroy_proc_cb = None
        self._destroy_hook = None
        self._pending_destroy_pid = 0
        self._pending_destroy_tid = 0
        # WM_APP requests posted from the Logic loop to the hook thread so the
        # transient destroy hook is registered/unregistered ON the hook thread
        # (SetWinEventHook hooks are owned by the thread that creates them).
        self._WM_REGISTER_DESTROY = 0x8000 + 1   # WM_APP + 1
        self._WM_UNREGISTER_DESTROY = 0x8000 + 2  # WM_APP + 2

    # -- lifecycle -----------------------------------------------------------
    def is_alive(self) -> bool:
        return bool(
            self._running
            and self._thread is not None
            and self._thread.is_alive()
        )

    def start(self) -> bool:
        """Start the hook thread; returns True once the message window is ready."""
        import threading

        if self._running:
            return True
        self._running = True
        self._thread_ready = threading.Event()
        self._thread = threading.Thread(
            target=self._message_loop, name="OverlayFocusHookThread", daemon=True,
        )
        self._thread.start()
        if not self._thread_ready.wait(timeout=3.0):
            logger.error(
                "OverlayFocusHookManager: timeout waiting for hook thread "
                "to become ready; overlay focus following disabled.",
            )
            self._running = False
            return False
        # wh-n29v.23.1: the readiness Event is signalled on BOTH success and
        # every startup-failure path (window-create error, foreground-hook
        # failure). On any failure the hook thread sets _running False (and runs
        # _teardown) BEFORE signalling ready, so a cleared _running here means
        # the message window or the foreground hook did not come up. Report that
        # as a failed start so the caller leaves focus following disabled rather
        # than storing a manager whose hook is dead.
        if not self._running:
            logger.warning(
                "OverlayFocusHookManager: hook thread reported a startup "
                "failure; overlay focus following disabled this session.",
            )
            return False
        logger.info("OverlayFocusHookManager: foreground hook thread ready.")
        return True

    def stop(self) -> None:
        """Stop the hook thread and unhook BOTH hooks (no leaked hooks)."""
        if not self._running:
            return
        self._running = False
        try:
            import win32con
            import win32gui

            if self._message_only_hwnd and win32gui.IsWindow(
                self._message_only_hwnd
            ):
                win32gui.PostMessage(
                    self._message_only_hwnd, win32con.WM_NULL, 0, 0,
                )
        except Exception as exc:  # noqa: BLE001 -- never block shutdown
            logger.debug(
                "OverlayFocusHookManager: failed to wake hook loop on stop: %s",
                exc,
            )
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=4.0)
            if thread.is_alive():
                logger.warning(
                    "OverlayFocusHookManager: hook thread did not stop within "
                    "the join timeout.",
                )
        self._thread = None

    # -- transient destroy hook (called from the Logic loop) ----------------
    def register_destroy_hook(self, *, pid: int, tid: int) -> bool:
        """Request registration of the transient destroy hook (while paused).

        Posts a request to the hook thread so the hook is created ON that
        thread (the thread that owns the message pump and the foreground hook).
        Scoped to ``pid`` / ``tid`` of the tracked foreground window. A
        ``pid``/``tid`` of 0 would register a SYSTEM-WIDE destroy hook, which
        is far too noisy; the caller must supply a real pair, and a 0 pair is
        rejected here as a no-op.

        Returns True only when the register request was ACCEPTED (the manager is
        alive, the message window exists, the pid/tid are real, and PostMessage
        succeeded). Returns False otherwise, so the caller does not record the
        hook as active when the request never reached the hook thread
        (wh-n29v.23.3). NOTE: a True here means the request was posted, not that
        the hook-thread ``SetWinEventHook(DESTROY)`` ultimately succeeded -- that
        call is asynchronous and best-effort. A later hook-thread failure
        degrades gracefully to the resume-time full-identity check, which design
        v4 makes the PRIMARY stale-overlay defence (the destroy hook is
        defence-in-depth).
        """
        if not self.is_alive() or not self._message_only_hwnd:
            return False
        if pid <= 0 or tid <= 0:
            logger.debug(
                "OverlayFocusHookManager: destroy-hook register skipped "
                "(pid=%s tid=%s); a real pid/tid is required to scope the hook.",
                pid, tid,
            )
            return False
        try:
            import win32api

            self._pending_destroy_pid = int(pid)
            self._pending_destroy_tid = int(tid)
            win32api.PostMessage(
                self._message_only_hwnd, self._WM_REGISTER_DESTROY, 0, 0,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "OverlayFocusHookManager: failed to post destroy-hook "
                "register request: %s", exc,
            )
            return False

    def unregister_destroy_hook(self) -> bool:
        """Request removal of the transient destroy hook (leaving paused).

        Returns True when the unregister request was posted, False otherwise.
        """
        if not self._message_only_hwnd:
            return False
        try:
            import win32api

            win32api.PostMessage(
                self._message_only_hwnd, self._WM_UNREGISTER_DESTROY, 0, 0,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "OverlayFocusHookManager: failed to post destroy-hook "
                "unregister request: %s", exc,
            )
            return False

    # -- hook-thread internals ----------------------------------------------
    def _message_loop(self) -> None:
        import ctypes
        from ctypes import wintypes

        import win32api
        import win32con
        import win32gui

        win_event_proc_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            wintypes.LONG,
            wintypes.LONG,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        def _foreground_proc(
            _hook, _event, hwnd, _id_object, _id_child, _thread, _time,
        ) -> None:
            # OS worker thread: marshal onto the Logic loop and do nothing else.
            if not self._running or not hwnd:
                return
            try:
                self._loop.call_soon_threadsafe(self._on_foreground, int(hwnd))
            except Exception:  # noqa: BLE001 -- loop may be closing
                pass

        def _destroy_proc(
            _hook, _event, hwnd, id_object, _id_child, _thread, _time,
        ) -> None:
            # Only top-level window (OBJID_WINDOW) destroys matter; child-object
            # destroys in the same process are filtered here so the loop is not
            # woken for every transient child teardown.
            if not self._running or not hwnd or id_object != _OBJID_WINDOW:
                return
            try:
                self._loop.call_soon_threadsafe(self._on_destroy, int(hwnd))
            except Exception:  # noqa: BLE001
                pass

        def _menu_popup_proc(
            _hook, event, _hwnd, _id_object, _id_child, _thread, _time,
        ) -> None:
            # wh-overlay-menu-close-stale: OS worker thread -- marshal the raw
            # event id onto the Logic loop and do nothing else. The pure
            # mapper (map_menu_popup_event) and the machine's state gating run
            # on the loop; no idObject filter here because the registered
            # range carries only the two pop-up ids and the mapper drops
            # anything else anyway.
            if not self._running or self._on_menu_popup is None:
                return
            try:
                self._loop.call_soon_threadsafe(
                    self._on_menu_popup, int(event),
                )
            except Exception:  # noqa: BLE001 -- loop may be closing
                pass

        # Hold strong references so the ctypes callbacks are not GC'd while the
        # hooks are live.
        self._foreground_proc_cb = win_event_proc_type(_foreground_proc)
        self._destroy_proc_cb = win_event_proc_type(_destroy_proc)
        self._menu_popup_proc_cb = win_event_proc_type(_menu_popup_proc)

        # PyWNDCLASS attribute assignments and the str-classname args, the proven
        # features/window_mover.py pattern. The pywin32 stub models the PyWNDCLASS
        # attributes as read-only properties and over-narrows the className/atom
        # params, so pyright flags these five calls as reportAttributeAccessIssue
        # / reportArgumentType even though they are correct at runtime.
        # window_mover.py makes the identical calls and carries the SAME five
        # errors unsuppressed (verified: it is not pyright-clean on its WNDCLASS
        # block); we suppress here to hold main.py at zero NEW pyright errors.
        # Precise `pyright: ignore[<rule>]` (not blanket `type: ignore`) so an
        # unrelated future error on any of these lines still surfaces.
        wndclass = win32gui.WNDCLASS()
        wndclass.hInstance = win32api.GetModuleHandle(None)  # pyright: ignore[reportAttributeAccessIssue]
        wndclass.lpszClassName = self._window_class_name  # pyright: ignore[reportAttributeAccessIssue]

        def _wnd_proc(hwnd_param, msg, wparam, lparam):
            if msg == self._WM_REGISTER_DESTROY:
                self._do_register_destroy(ctypes, win32api)
                return 0
            if msg == self._WM_UNREGISTER_DESTROY:
                self._do_unregister_destroy(ctypes)
                return 0
            if msg == win32con.WM_DESTROY:
                win32gui.PostQuitMessage(0)
                return 0
            return win32gui.DefWindowProc(hwnd_param, msg, wparam, lparam)

        wndclass.lpfnWndProc = _wnd_proc  # pyright: ignore[reportAttributeAccessIssue]
        try:
            try:
                win32gui.UnregisterClass(
                    wndclass.lpszClassName, wndclass.hInstance,  # pyright: ignore[reportArgumentType]
                )
            except Exception:  # noqa: BLE001
                pass
            class_atom = win32gui.RegisterClass(wndclass)
            self._message_only_hwnd = win32gui.CreateWindowEx(
                0, class_atom, "OverlayFocusHookMessageOnly", 0, 0, 0, 0, 0,  # pyright: ignore[reportArgumentType]
                win32con.HWND_MESSAGE, None, wndclass.hInstance, None,
            )
            if not self._message_only_hwnd:
                logger.error(
                    "OverlayFocusHookManager: failed to create message-only "
                    "window (error %s).", win32api.GetLastError(),
                )
                # wh-n29v.23.1: RegisterClass already succeeded, so _teardown
                # must run to UnregisterClass it (a bare return leaked the
                # WNDCLASS). _running False before signalling ready makes
                # start() report the failure.
                self._running = False
                self._teardown(ctypes, win32api, win32gui)
                if self._thread_ready is not None:
                    self._thread_ready.set()
                return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "OverlayFocusHookManager: error creating message window: %s",
                exc,
            )
            self._running = False
            self._teardown(ctypes, win32api, win32gui)
            if self._thread_ready is not None:
                self._thread_ready.set()
            return

        self._foreground_hook = ctypes.windll.user32.SetWinEventHook(
            _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, 0,
            self._foreground_proc_cb, 0, 0,
            win32con.WINEVENT_OUTOFCONTEXT | win32con.WINEVENT_SKIPOWNPROCESS,
        )
        if not self._foreground_hook:
            # wh-n29v.23.1: the foreground hook is the manager's whole purpose.
            # If it fails, tear down (unhook nothing, destroy the window,
            # unregister the class) and report a failed start instead of
            # entering the pump with a dead hook.
            logger.error(
                "OverlayFocusHookManager: SetWinEventHook(FOREGROUND) failed "
                "(error %s); overlay focus following disabled.",
                win32api.GetLastError(),
            )
            self._running = False
            self._teardown(ctypes, win32api, win32gui)
            if self._thread_ready is not None:
                self._thread_ready.set()
            return

        # wh-overlay-menu-close-stale: register the menu pop-up hook on the
        # exact two-id range (0x0006..0x0007; no other WinEvent id lies
        # between). SKIPOWNPROCESS excludes only THIS (Logic) process, which
        # owns no menus -- WheelHouse's tray and editor menus live in the GUI
        # process, so their events DO reach this hook and rely on the config
        # gate + shared debounce + closed-state no-op for containment
        # (wh-overlay-nested-dupes.1.3). Non-fatal on failure: the foreground
        # hook (the manager's purpose) is already up, so a dead menu hook only
        # degrades the menu-close re-walk back to the pre-fix behaviour.
        if self._on_menu_popup is not None:
            from services.wheelhouse.overlay_focus_hooks import (
                EVENT_SYSTEM_MENUPOPUPEND,
                EVENT_SYSTEM_MENUPOPUPSTART,
            )

            self._menu_popup_hook = ctypes.windll.user32.SetWinEventHook(
                EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND, 0,
                self._menu_popup_proc_cb, 0, 0,
                win32con.WINEVENT_OUTOFCONTEXT
                | win32con.WINEVENT_SKIPOWNPROCESS,
            )
            if not self._menu_popup_hook:
                logger.warning(
                    "OverlayFocusHookManager: SetWinEventHook(MENUPOPUP) "
                    "failed (error %s); overlay menu-close refresh disabled.",
                    win32api.GetLastError(),
                )

        if self._thread_ready is not None:
            self._thread_ready.set()

        msg = wintypes.MSG()
        while self._running:
            if ctypes.windll.user32.PeekMessageW(
                ctypes.byref(msg), self._message_only_hwnd, 0, 0,
                win32con.PM_REMOVE,
            ):
                if msg.message == win32con.WM_QUIT:
                    break
                ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
                ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))
            if not self._running:
                break
            import time

            time.sleep(0.02)

        self._teardown(ctypes, win32api, win32gui)

    def _do_register_destroy(self, ctypes, win32api) -> None:
        # Runs on the hook thread (posted via WM_APP). Replace any existing
        # transient hook with one scoped to the pending pid/tid.
        self._do_unregister_destroy(ctypes)
        pid = self._pending_destroy_pid
        tid = self._pending_destroy_tid
        if pid <= 0 or tid <= 0:
            return
        self._destroy_hook = ctypes.windll.user32.SetWinEventHook(
            _EVENT_OBJECT_DESTROY, _EVENT_OBJECT_DESTROY, 0,
            self._destroy_proc_cb, pid, tid, 0,  # WINEVENT_OUTOFCONTEXT == 0
        )
        if not self._destroy_hook:
            logger.warning(
                "OverlayFocusHookManager: SetWinEventHook(DESTROY) failed for "
                "pid=%s tid=%s (error %s).", pid, tid, win32api.GetLastError(),
            )

    def _do_unregister_destroy(self, ctypes) -> None:
        if self._destroy_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._destroy_hook)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "OverlayFocusHookManager: UnhookWinEvent(destroy) error: %s",
                    exc,
                )
            self._destroy_hook = None

    def _teardown(self, ctypes, win32api, win32gui) -> None:
        if self._foreground_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._foreground_hook)
            except Exception:  # noqa: BLE001
                pass
            self._foreground_hook = None
        if self._menu_popup_hook:
            try:
                ctypes.windll.user32.UnhookWinEvent(self._menu_popup_hook)
            except Exception:  # noqa: BLE001
                pass
            self._menu_popup_hook = None
        self._do_unregister_destroy(ctypes)
        if self._message_only_hwnd and win32gui.IsWindow(self._message_only_hwnd):
            try:
                win32gui.DestroyWindow(self._message_only_hwnd)
            except Exception:  # noqa: BLE001
                pass
        self._message_only_hwnd = None
        try:
            win32gui.UnregisterClass(
                self._window_class_name, win32api.GetModuleHandle(None),
            )
        except Exception:  # noqa: BLE001
            pass
        logger.debug("OverlayFocusHookManager: hook thread torn down.")


class LogicController:
    """Main application logic coordinator."""

    def __init__(self, app: WheelHouseApp, config_service: ConfigService, shutdown_event: Event, event_bus: EventBus, service_manager: ServiceManager, state_manager: StateManager, gui_shm_name: str = None):
        """
        Initializes the LogicController, the central orchestrator for the application's logic.

        Args:
            app (WheelHouseApp): The main application object managing process communication.
            config_service (ConfigService): The application's configuration service.
            shutdown_event (Event): A multiprocessing event to signal and coordinate shutdown across processes.
            event_bus (EventBus): The central event bus for decoupled communication.
            service_manager (ServiceManager): Manages the lifecycle of background services.
            state_manager (StateManager): Manages and synchronizes the application's state with the GUI.
        """
        logger.info("Initializing LogicController instance.")
        self.app = app
        self.shutdown_event = shutdown_event
        self.event_bus = event_bus
        self.service_manager = service_manager
        self.state_manager = state_manager
        self.config_service = config_service
        
        self.loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self.shutdown_requested = False
        self._shutdown_complete = False
        self.background_tasks: List[asyncio.Task] = []
        self.gui_shm_name = gui_shm_name

        # wh-iycks: Logic-side bounded TTL cache mapping correlation_token
        # to the rejection's identifying tuple. Populated when the Logic
        # process forwards a text_target_rejected event to the GUI;
        # consulted when the GUI emits a try_anyway_clicked event. The
        # dictation text is NOT stored here (privacy contract; Input owns
        # the text cache). wh-mv5ih reads this same cache from
        # forward_retry_dictation_by_token to map a verified retry back
        # to the rejected control's identity tuple for the RetryVerified
        # event.
        from shared.rejection_token_cache import RejectionTokenCache
        self.rejection_token_cache = RejectionTokenCache()

        # wh-82lnx: per-correlation_token dedup so a duplicate Try-it-anyway
        # click on the same token does not double-increment the click
        # counter. Same 60-second TTL as the rejection cache; checked in
        # forward_retry_dictation_by_token before publishing RetryVerified.
        self.consumed_retry_tokens = ConsumedTokenSet()

        # wh-82lnx.2.2: per-correlation_token in-flight set. Reserves
        # the token at the top of forward_retry_dictation_by_token,
        # before the Input-process IPC, so two concurrent clicks on
        # the same token do not BOTH dispatch retry_dictation_by_token
        # and BOTH paste into the focused control. The reservation is
        # released in a finally block on every exit path so a failed
        # or unverified retry does not permanently lock the token.
        # Distinct from consumed_retry_tokens: in_flight gates against
        # concurrency, consumed gates against post-success replays.
        self._in_flight_retry_tokens: set[str] = set()

        # wh-82lnx: per-tuple verified-retry counter with best-effort
        # persistence. Subscribes to RetryVerified on the EventBus,
        # publishes RetryThresholdReached when a tuple's count meets
        # the soft-allow threshold. wh-bqv9c will subscribe to the
        # threshold event and surface the user-facing prompt.
        # Path derives from utils.system.get_user_data_dir (wh-k8ef) so
        # it stays alongside soft_allow_tuples.toml (the wh-9weum
        # Phase 3 file) in both the source checkout and a frozen build.
        counters_path = self._resolve_pending_counters_path()
        self.click_counter = ClickCounter(
            event_bus=self.event_bus,
            persistence_path=counters_path,
            threshold=self._read_soft_allow_threshold(),
        )
        self.click_counter.subscribe()
        self.click_counter.load_from_disk()

        # wh-vdt1t / wh-27gvv: "user said no" suppression set. Populated
        # at startup from soft_allow_declined_tuples.toml via
        # _load_declined_tuples, and extended at runtime by
        # _handle_grant_prompt_no_clicked when the user clicks No on
        # the three-strikes follow-up toast. Consulted by
        # _on_retry_threshold_reached BEFORE forwarding to the GUI;
        # suppressed tuples drop the forward silently so the toast does
        # not re-fire, on this machine, until the user deletes the
        # entry from the file.
        #
        # The set started as in-memory-only (wh-vdt1t) and gained
        # disk-backed persistence in wh-27gvv. The handler now writes
        # to disk first and only mutates the set on a successful write
        # so the disk and the running process agree (or a restart can
        # recover any decline that landed on disk).
        self._grant_prompt_no_suppressed: set[tuple[str, str, str]] = set()
        self._load_declined_tuples()

        # wh-bqv9c: forward RetryThresholdReached events to the GUI as
        # text_target_grant_prompt actions on the state queue. The GUI
        # owns its own per-tuple per-session dedup as defense-in-depth;
        # Logic owns the authoritative suppression for the No path
        # (see _grant_prompt_no_suppressed above).
        self._subscribe_grant_prompt_forwarder()

        # wh-g2-refactor.18 (Sections 2, 5, 6): Logic-side per-word
        # insert and retract IPC against the persistent editor. The two
        # pending-request maps store (future, generation) tuples so the
        # rebuild fan-out can fail every stale future in bulk. The
        # rebuild-handler subscribes to both maps and bumps
        # ``editor_generation_observed`` whenever the GUI emits an
        # editor_rebuilt notification.
        from services.wheelhouse.shared.editor_pending_request import (
            EditorPendingRequestMap,
        )
        from services.wheelhouse.shared.editor_rebuilt_handler import (
            LogicRebuildFanout,
        )

        self._insert_pending = EditorPendingRequestMap()
        self._retract_pending = EditorPendingRequestMap()
        self._editor_rebuild_fanout = LogicRebuildFanout(
            pending_maps=(self._insert_pending, self._retract_pending),
            initial_generation=0,
        )
        # Per-word timeouts. Section 5 specifies 1.0 s for insert (hot
        # path) and Section 2 specifies 2.0 s for retract (GUI Qt-side
        # ledger validation can dwell while a partial-trim runs). Both
        # are working baselines the design names as configurable; a
        # future tunable would key off [g2.editor] in config.toml.
        self._insert_timeout_s: float = 1.0
        self._retract_timeout_s: float = 2.0

        # wh-c169t: whether the opt-in Windows screen-reader flag was SET at
        # startup (i.e. the user opted in). Initialised here so it always
        # exists even if startup aborts before the apply call; shutdown reads
        # it to decide whether to clear the flag. Self-recovery clears (when
        # off) do not set this True -- only a real enable does.
        self._screen_reader_flag_enabled: bool = False

        # wh-tab7j (wh-l4h.1 Phase 1): validated voice-clicking config and the
        # Logic-side snapshot-summary cache. ClickConfig.from_raw is the only
        # never-raising reader of the [click] block; read it once at init so
        # the click_element action gate and the awaiter both see one config.
        # The cache retains the WalkSnapshotSummary forwarded for the Phase 1.5
        # numbered overlay, keyed by snapshot_id, with TTL from the same
        # [click] block (snapshot_ttl_seconds). The one-shot disabled-by-config
        # notice is suppressed after the first attempt per session.
        from services.wheelhouse.ui.click_config import ClickConfig
        from services.wheelhouse.click_snapshot_summary_cache import (
            ClickSnapshotSummaryCache,
        )
        from services.wheelhouse.click_overlay_state import (
            ClickOverlayStateMachine,
            OverlayState,
        )

        self.click_config = ClickConfig.from_raw(
            self.config_service.get("click", {})
        )
        self.click_snapshot_summary_cache = ClickSnapshotSummaryCache(
            ttl_seconds=float(self.click_config.snapshot_ttl_seconds),
        )
        self._click_disabled_notice_shown: bool = False

        # wh-n29v.17: Logic-side toggle state machine for the Phase 1.5
        # numbered overlay. Constructed with its defaults (walk 2500ms /
        # paint 1000ms); the [click] overlay-timeout config plumbing is a
        # separate unshipped bead, so no config keys are read here. This
        # machine is the single routing source of truth for "show numbers" /
        # "hide numbers" and numeric "click N" -- see forward_click_element
        # and handle_overlay_command. The effect-PERFORMING integration (real
        # IPC dispatch of build/paint/clear, pin/unpin, the 200ms hold timer,
        # the per-state timeout timers, the real click_snapshot_item dispatch)
        # is owned by the not-yet-shipped integration bead wh-h9a8v2; this
        # slice hands every collected effect / decision to a documented stub
        # seam (_perform_overlay_effects / _dispatch_snapshot_item_click /
        # _hold_click_n).
        self.click_overlay_state = ClickOverlayStateMachine()

        # wh-n29v.95: effect-performing overlay integration state.
        #
        # ``_overlay_effect_lock`` serializes whole effect batches so the async
        # effect dispatch (build/paint/clear/pin/unpin/notice/timer) ships in
        # machine-return order even when ``_perform_overlay_effects`` is invoked
        # concurrently from multiple in-flight asyncio tasks (the
        # overlay_state_changed acks, the build-response feed, the timer feed).
        # ``machine.apply`` commits the state transition synchronously BEFORE
        # the performer runs, so machine state stays consistent; this lock
        # guarantees dispatch ORDERING -- a not-yet-completed clear from one ack
        # is never reordered against a paint/clear from a later ack
        # (wh-n29v.70.2). asyncio.Lock is FIFO, so batches dispatch in the order
        # they were scheduled on this single Logic loop.
        #
        # ``_overlay_timer`` is the single live per-state timeout handle (exactly
        # one timer is armed at a time -- the machine emits CANCEL_TIMER before a
        # new ARM_TIMER where needed); ``_overlay_timer_pair`` is the
        # (overlay_session_id, paint_generation) the timer was armed for, fed
        # back as the TIMEOUT event's pair on fire so the machine's generation
        # gate drops a timer for a superseded generation.
        # ``_overlay_hold_timer`` is the single live 200ms "click N" hold handle.
        self._overlay_effect_lock = asyncio.Lock()
        self._overlay_timer: "asyncio.TimerHandle | None" = None
        self._overlay_timer_pair: "tuple[int, int] | None" = None
        # The OverlayState the live per-state timer guards (WALK_IN_FLIGHT /
        # PAINT_IN_FLIGHT / REFRESH_IN_FLIGHT), or None when no timer is armed.
        # Tracked so a regression test can confirm a build-response feed does not
        # leave a stale WALK timer armed after the machine has moved to
        # paint_in_flight (wh-n29v.96.4).
        self._overlay_armed_timer_state: "OverlayState | None" = None
        self._overlay_hold_timer: "asyncio.TimerHandle | None" = None
        # wh-n29v.96.2: periodic keepalive timer for a quiescent painted/paused
        # overlay. PAINTED/PAUSED are steady NO_TIMEOUT states with no recurring
        # pin/paint, so without a periodic re-put a visible overlay idle past the
        # snapshot TTL would age out and 'click N' would misreport 'no badge N'.
        # The timer re-puts the machine's currently-pinned summary every
        # ``_overlay_keepalive_interval_s`` and reschedules itself; it is armed on
        # entry to PAINTED/PAUSED and cancelled on leaving them.
        self._overlay_keepalive_timer: "asyncio.TimerHandle | None" = None
        self._overlay_keepalive_interval_s: float = max(
            float(self.click_config.snapshot_ttl_seconds) / 2.0, 1.0
        )

        # wh-n29v.121: proactive refresh for a painted overlay over a browser
        # window. Dynamic Chromium pages shift layout while the user reads, so
        # cached badge positions go stale with no focus change, menu event, or
        # click to trigger the reactive re-walks. Each keepalive tick checks:
        # PAINTED + tracked window in the effective browser-process set + last
        # PAINTED entry older than the trust window -> feed one FOCUS_CHANGE
        # (a REFRESH in PAINTED). 0 disables. The cadence is quantized to the
        # keepalive tick, so the real window is this value rounded up to the
        # next tick (documented on the config key).
        self._overlay_browser_refresh_seconds: float = float(
            self.click_config.overlay_browser_refresh_seconds
        )
        from services.wheelhouse.ui.browser_dom_corrections import (
            effective_browser_processes,
        )
        # Lowercased + de-duplicated by the helper -- the same effective list
        # ElementFinder uses for DOM corrections, so "browser" means the same
        # thing on both sides of the IPC boundary.
        self._overlay_browser_process_set: frozenset[str] = frozenset(
            effective_browser_processes(
                list(self.click_config.browser_processes),
                list(self.click_config.browser_processes_extend),
            )
        )
        # Monotonic timestamp of the LATEST transition into PAINTED (stamped
        # in _apply_overlay_event), so the refresh measures age from the most
        # recent paint, not the first. None until the first paint.
        self._overlay_last_paint_monotonic: "float | None" = None
        # wh-overlay-fixqueue-review.1: multiplier on the proactive-refresh
        # trust window. A FAILED proactive refresh restores the prior
        # snapshot by re-entering PAINTED (re-stamping the paint age), so
        # without this a window whose walk consistently failed would be
        # re-walked every window forever, stalling the Input process's
        # serial command loop each time. Doubles on each failed proactive
        # refresh (capped in _apply_overlay_event), resets on a successful
        # one and on session close.
        self._overlay_browser_refresh_backoff: int = 1
        # True only while _maybe_overlay_browser_refresh is inside its
        # apply, so the REFRESH_IN_FLIGHT entry edge can attribute the
        # refresh to the proactive trigger (vs the focus/menu hooks).
        self._overlay_in_proactive_apply: bool = False
        self._overlay_refresh_started_proactive: bool = False
        self._overlay_refresh_entry_pin: "str | None" = None
        # wh-overlay-fixqueue-review.2: (prior_snapshot_id, swap_monotonic)
        # recorded when a PROACTIVE refresh swaps in a new snapshot;
        # consumed by the renumber guard in forward_click_element so a
        # "click N" spoken against the pre-swap badges is not silently
        # resolved against the renumbered overlay.
        self._overlay_proactive_swap: "tuple[str | None, float] | None" = None

        # wh-n29v.21: Logic-side Win32 focus hooks for the numbered overlay.
        # The FOREGROUND hook feeds debounced FOCUS_CHANGE events into the
        # machine; the TRANSIENT DESTROY hook (registered only while paused,
        # scoped to the tracked window's pid/tid) feeds FOCUSED_HWND_DESTROYED.
        # The pure decision logic (debounce window, generation supersession,
        # full-identity comparison, raw-event -> OverlayEvent mapping) lives in
        # overlay_focus_hooks.py; the raw SetWinEventHook seam is
        # OverlayFocusHookManager above. The hook thread is started in main()
        # only when voice clicking is enabled, and stopped in shutdown(). The
        # debounce interval comes from the VALIDATED overlay config value
        # (self.click_config.overlay_focus_debounce_ms, range [0, 5000], default
        # 250) so the Logic and Input processes derive the SAME debounce from
        # the same raw [click] block (wh-n29v.66 Phase 1.5 parity invariant).
        self._overlay_focus_debouncer = self._build_overlay_focus_debouncer()
        # Pending trailing settle re-fire timer (wh-overlay-nested-dupes.1.1):
        # a coalesced focus/menu event arms this one-shot loop timer for the
        # debounce-window remainder so the FINAL event of a burst always
        # produces one re-walk. None when no settle is pending.
        self._overlay_settle_handle = None
        # The hook manager is created lazily in main() (it spawns a thread and
        # creates a Win32 window); None until started, and on a non-Windows or
        # disabled run it stays None.
        self._overlay_focus_hooks: "OverlayFocusHookManager | None" = None
        # Identity of the foreground window the overlay was built for. The
        # resume-time full-identity check and the transient destroy hook both
        # read this. It is NOT set by this slice (wh-n29v.21): the
        # effect-performing overlay integration (wh-h9a8v2) must capture it via
        # ``_capture_overlay_foreground_identity()`` at the moment the overlay
        # becomes visible/paused (when it pins the snapshot) and clear it to
        # ``None`` on entry to ``closed``. Until that integration lands the
        # field stays ``None``, so the destroy hook stays unregistered and the
        # resume check returns ``False`` (re-walk) -- the safe defaults. This
        # cross-slice dependency is tracked as an acceptance criterion on
        # wh-h9a8v2.
        from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity
        self._overlay_tracked_identity: "ForegroundIdentity | None" = None
        # wh-overlay-snapshot-keepalive (trigger B): the foreground-window
        # identity each pinned snapshot was built for, keyed by snapshot_id.
        # Unlike the single ``_overlay_tracked_identity`` (which always names the
        # LATEST pin), this remembers per snapshot, so during a focus-change
        # refresh -- where the new window's snapshot is already pinned but the
        # PRIOR (still-visible) snapshot belongs to the window that lost focus --
        # routing can tell the visible snapshot's window from the current
        # foreground and HOLD a "click N" instead of dispatching a click Input
        # would reject. Populated at the pin point, dropped at unpin, and cleared
        # wholesale on entry to ``closed``.
        self._overlay_snapshot_window_identity: "dict[str, ForegroundIdentity]" = {}
        # Whether the transient destroy hook is currently registered, so the
        # paused-enter/leave reconciliation does not double-register or
        # double-unregister.
        self._overlay_destroy_hook_active: bool = False
        # wh-n29v.101.1: a successful 'click N' whose ok reply arrives while the
        # overlay is REFRESH_IN_FLIGHT cannot refresh immediately -- the machine
        # returns HELD for CLICK_COMPLETE in that state, and the pair captured at
        # click-dispatch already names the in-flight refresh generation. Record
        # that pair here so the post-click refresh is REPLAYED once the same
        # generation settles back into PAINTED, instead of being dropped (which
        # would leave the in-flight generation painting a pre-click snapshot with
        # no later refresh, so the next 'click N' resolves against a stale
        # snapshot -- the exact wrong-target risk this slice exists to prevent).
        # ``_reconcile_overlay_pending_postclick_refresh`` consumes it on entry to
        # PAINTED at the matching pair and clears it on supersede (the live pair
        # moved past the recorded one) or session end (entry to closed), so a
        # stale pending refresh can never fire on a newer overlay. ``None`` means
        # no post-click refresh is pending.
        self._overlay_pending_postclick_refresh: "tuple[int, int] | None" = None

        # wh-n29v.111: the auto-open item_id_filter, stashed by
        # forward_click_element when it feeds an AUTO_OPEN OverlayEvent and read
        # back by _overlay_dispatch_build when the AUTO_OPEN DISPATCH_BUILD effect
        # runs. The DISPATCH_BUILD Effect carries only snapshot_id (the reuse
        # target), so the finalist filter -- sourced from
        # ClickElementResponse.ambiguous_item_ids -- rides here in the integration
        # layer, NOT on a state-machine Effect. Keyed by the auto-open
        # (overlay_session_id, paint_generation) so a superseding build (a fresh
        # start_overlay_walk) cannot pick up a stale filter; the machine holds at
        # most one auto-open in flight, so a single slot suffices. ``None`` means
        # no auto-open filter is pending.
        self._overlay_auto_open_filter: (
            "tuple[tuple[int, int], list[str]] | None"
        ) = None

        # wh-r3xy1: first-use discovery hint for the screen-reader-flag
        # opt-in. The first voice click into a Chromium-family window while
        # `[click] enable_screen_reader_flag` is false surfaces a one-shot
        # info notice through the existing GUI state queue (no new IPC
        # channel). The tracker owns the dismiss-or-three-subsequent-clicks
        # suppression and the durable record file (data/
        # click_first_use_hint_shown.toml). Initialised lazily on the first
        # click so a test or a no-click session never touches disk; see
        # _first_use_hint_tracker().
        self._first_use_hint: "FirstUseHintTracker | None" = None

    def _read_soft_allow_threshold(self) -> int:
        """Read [ui_actions.text_target].soft_allow_threshold from config.

        Defaults to 3 if the key is missing or invalid (wh-bqv9c spec).
        Values < 1 are clamped to 1 so the threshold can never be
        unreachable.
        """
        raw = self.config_service.get(
            "ui_actions.text_target.soft_allow_threshold", 3,
        )
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "soft_allow_threshold config value %r is not an int; "
                "falling back to 3", raw,
            )
            return 3
        if value < 1:
            logger.warning(
                "soft_allow_threshold=%d is below 1; clamping to 1", value,
            )
            return 1
        return value

    def _subscribe_grant_prompt_forwarder(self) -> None:
        """Register the RetryThresholdReached -> GUI forwarder (wh-bqv9c).

        Called once at LogicController init time, after the click
        counter has subscribed to RetryVerified. The forwarder runs on
        the EventBus dispatch tick that follows a counter increment;
        it has no shared state with the counter, so a slow forwarder
        does not block subsequent counter increments.
        """
        self.event_bus.subscribe(
            RetryThresholdReached, self._on_retry_threshold_reached,
        )

    async def _on_retry_threshold_reached(
        self, event: RetryThresholdReached,
    ) -> None:
        """Forward a RetryThresholdReached event to the GUI as a
        text_target_grant_prompt action (wh-bqv9c).

        The GUI's queue listener routes by ``action`` and calls
        ``_show_grant_prompt_toast``, which renders the three-strikes
        follow-up toast. The GUI owns per-tuple per-session dedup;
        this forwarder fires on every event the counter publishes.

        Failure handling: if ``state_manager`` is missing or its
        ``state_to_gui_queue`` is unavailable, log a warning and drop
        the event. A queue ``put_nowait`` failure is likewise treated
        as a drop. The user will see the standard rejection toast
        the next time the same target rejects, so a missed grant
        prompt does not lock them out of the recovery flow.

        Privacy: this method NEVER sees dictation text and never
        forwards a correlation_token. The payload carries platform
        metadata (process / class / control type / app friendly name)
        and the per-tuple counter value only.
        """
        from services.wheelhouse.shared.text_target_grant_prompt import (
            MSG_TYPE,
            TextTargetGrantPromptEvent,
        )

        if self.state_manager is None or not hasattr(
            self.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "grant_prompt forwarder dropped event: no "
                "state_manager.state_to_gui_queue available",
            )
            return

        # wh-vdt1t: consult the per-run "user said no" suppression set
        # before forwarding. A suppressed tuple drops the forward
        # silently; the user already declined this run.
        tuple_key = (
            event.process_name, event.class_name, event.control_type,
        )
        if tuple_key in self._grant_prompt_no_suppressed:
            logger.debug(
                "grant_prompt forwarder suppressed (user said no this run): "
                "tuple=%s", tuple_key,
            )
            return

        payload = TextTargetGrantPromptEvent(
            process_name=event.process_name,
            class_name=event.class_name,
            control_type=event.control_type,
            app_friendly_name=event.app_friendly_name,
            count=event.count,
        ).to_dict()
        # The schema dataclass labels its dict with ``"type"`` so the
        # input -> logic schemas in the same package are uniform. The
        # GUI queue listener dispatches by ``"action"``, so rebrand
        # before posting. Carrying both keys would let a stale
        # listener accidentally double-dispatch the message.
        payload.pop("type", None)
        gui_msg = {"action": MSG_TYPE, **payload}

        try:
            self.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        except Exception as exc:
            logger.warning(
                "grant_prompt forwarder dropped event: "
                "state_to_gui_queue put_nowait failed: %s", exc,
            )

    def handle_exit_signal(self, sig, frame=None):
        """
        Handles operating system signals (e.g., SIGINT, SIGTERM) to initiate a graceful shutdown.
        This method is registered as a signal handler.

        Args:
            sig: The signal number.
            frame: The current stack frame (unused).
        """
        logger.warning(f"Received signal {sig}. Initiating shutdown.")
        self.request_shutdown()

    def async_exception_handler(self, loop, context):
        """
        Global exception handler for the asyncio event loop.

        It logs any exceptions that are not properly caught in asyncio tasks,
        preventing them from being silenced. It triggers a shutdown for any
        non-cancellation error -- EXCEPT the "exception was never
        retrieved" reports asyncio produces from a NON-Task future's
        finalizer at garbage collection (wh-handler-shutdown-policy).
        Those describe an already-completed executor or wrapped future
        that nothing awaits, arriving at an arbitrary GC-determined
        time; they are a logging gap in the producer, not a live
        failure. On 2026-07-10 a transient console-probe timeout leaked
        through a fire-and-forget pre-warm future and the unconditional
        shutdown ended an 18-hour session while the user was idle.

        The same GC-time report for an asyncio.Task stays FATAL
        (wh-log-crash-fixes.3.1): several modules still start
        background work with raw create_task (StateManager broadcasts,
        WheelHouseApp demux/sender loops, STTManager transcription
        loop), and a Task that died unawaited means that work is gone
        and the app is silently degraded -- a dead STT loop is worse
        than a launcher-supervised restart. Task failures created via
        create_task_with_error_handling shut down deterministically
        through _handle_task_completion before GC ever reports them.

        Args:
            loop: The asyncio event loop.
            context: A dictionary containing exception details.
        """
        message = context.get("message", "")
        msg = context.get("exception", message)
        if isinstance(context.get('exception'), asyncio.CancelledError):
            return
        if (
            "exception was never retrieved" in message
            and not isinstance(context.get("future"), asyncio.Task)
        ):
            logger.error(
                f"Global asyncio exception (unretrieved background "
                f"failure; not treated as fatal): {msg}",
                exc_info=context.get('exception'),
            )
            return
        logger.error(f"Global asyncio exception: {msg}", exc_info=context.get('exception'))
        self.request_shutdown()
            
    def request_shutdown(self):
        """
        Initiates a graceful shutdown of the application.

        Signals the main loop to exit. The actual teardown happens in shutdown(),
        which runs in the finally block of main(). This ensures services are stopped
        in the correct order (STT shutdown via WebSocket before WebSocket is closed).
        """
        if self.shutdown_event and not self.shutdown_event.is_set():
            logger.info("Shutdown requested. Signaling main loop to exit...")
            self.shutdown_event.set()

    async def restart_program(self):
        """
        Initiates a program restart by creating a flag file and then shutting down.
        The launcher script checks for this flag file on exit to determine whether to restart.
        """
        logger.info("Restart requested. Creating restart flag and shutting down.")
        try:
            flag_path = os.path.join(get_app_data_path(), "wheelhouse.restart")
            await asyncio.to_thread(lambda: open(flag_path, "w").write("restart"))
            self.request_shutdown()
        except IOError as e:
            logger.error(f"Could not create restart flag file: {e}")

    async def restart_stt_service(self):
        """
        Sends a restart command to the STT server via WebSocket.
        
        This command is sent to the remote STT service, which may be running
        on a different machine. The STT server will perform a graceful restart
        of its transcription service.
        """
        logger.info("STT service restart requested. Sending command to STT server...")
        try:
            if not hasattr(self.app, 'websocket_manager') or not self.app.websocket_manager:
                logger.error("Cannot restart STT service: WebSocket manager not available")
                return
            
            await self.app.websocket_manager.send_command_to_stt("restart_service")
            logger.info("STT restart command sent successfully")
        except Exception as e:
            logger.error(f"Failed to send STT restart command: {e}", exc_info=True)

    async def hard_restart_stt_service(self):
        """
        Sends a hard restart command to the STT server via WebSocket.
        
        Unlike restart_stt_service (which hot-reloads config), this triggers
        a full process restart. Use when device settings have changed and
        require complete reinitialization.
        """
        logger.info("STT HARD restart requested. Sending command to STT server...")
        try:
            if not hasattr(self.app, 'websocket_manager') or not self.app.websocket_manager:
                logger.error("Cannot hard restart STT service: WebSocket manager not available")
                return
            
            await self.app.websocket_manager.send_command_to_stt("hard_restart_service")
            logger.info("STT hard restart command sent successfully")
        except Exception as e:
            logger.error(f"Failed to send STT hard restart command: {e}", exc_info=True)

    async def toggle_interim_results(self):
        """Toggle whether STT sends interim (partial) results or only final results."""
        try:
            if not hasattr(self.app, 'websocket_manager') or not self.app.websocket_manager:
                logger.error("Cannot toggle interim results: WebSocket manager not available")
                return

            # Toggle the state in both StateManager and WebSocketManager
            self.state_manager.interim_results_enabled = not self.state_manager.interim_results_enabled
            enabled = self.state_manager.interim_results_enabled

            # Keep WebSocketManager in sync (for new client connections)
            self.app.websocket_manager.interim_results_enabled = enabled

            await self.app.websocket_manager.send_command_to_stt(
                "set_interim_results", enabled=enabled
            )
            logger.info(f"Interim results {'enabled' if enabled else 'disabled'}")

            # Send state update to GUI
            self.state_manager.send_state_update()
        except Exception as e:
            logger.error(f"Failed to toggle interim results: {e}", exc_info=True)

    async def toggle_log_level(self):
        """Toggles the logging level and propagates to InputProcess."""
        root = logging.getLogger()
        new_level = logging.DEBUG if root.getEffectiveLevel() != logging.DEBUG else logging.INFO
        root.setLevel(new_level)
        # Keep websockets logging at INFO to avoid excessive noise
        logging.getLogger("websockets").setLevel(logging.INFO)
        level_name = logging.getLevelName(new_level)
        logger.info("Logging level set to %s", level_name)

        # Propagate to InputProcess
        await self.app.send_command("set_log_level", {"level": level_name})

        # Propagate to STT providers
        self.app.websocket_manager.set_log_level(level_name)
        await self.app.websocket_manager.send_command_to_stt("set_log_level", level=level_name)

        # Update GUI state
        self.state_manager.debug_mode = (new_level == logging.DEBUG)
        self.state_manager.send_state_update()

    async def handle_transcribed_text(self, text: str):
        """
        Processes transcribed text from the speech-to-text server.

        If speech processing is enabled in the state manager, this method forwards
        the text to the SpeechHandler for command processing.

        Args:
            text (str): The transcribed text.
            
        """
        if not self.state_manager.speech_enabled:
            return
        if self.service_manager.speech_handler:
            await self.service_manager.speech_handler.process_transcription(text)

    async def _handle_stt_transcript(self, event) -> None:
        """Handle transcript events from in-process STTManager.
        
        :flow: In-Process STT
        :step: 2
        :description: Bridges TranscriptEvent from STTManager to speech processing pipeline
        :data_in: TranscriptEvent with text, is_final, and utterance_id fields
        :data_out: WordEvent objects queued to word_queue for SpeechProcessor
        :notes: Converts transcript text into WordEvent objects matching the WebSocket
                manager pattern. Each word is annotated with utterance boundary flags.
        
        Args:
            event: TranscriptEvent from the STT provider.
        """
        if not self.state_manager.speech_enabled:
            return
        
        if not event.text:
            return
        
        # Get word_queue from WebSocket manager (shared with SpeechProcessor)
        word_queue = getattr(self.app.websocket_manager, 'word_queue', None)
        if not word_queue:
            logger.warning("word_queue not available - cannot route transcript")
            return
        
        # Import WordEvent to create word events
        from speech.word_event import WordEvent
        
        # Split text into words and create WordEvents
        words = event.text.strip().split()
        if not words:
            return
        
        for i, word in enumerate(words):
            is_first = (i == 0)
            is_last = (i == len(words) - 1) and event.is_final
            
            word_event = WordEvent(
                word=word,
                start_of_utterance=is_first,
                end_of_utterance=is_last,
                utterance_id=event.utterance_id,
            )
            await word_queue.put(word_event)
        
        # If this is a final transcript, send end marker
        if event.is_final:
            end_marker = WordEvent(
                word="",
                start_of_utterance=False,
                end_of_utterance=True,
                utterance_id=event.utterance_id,
                is_utterance_end_marker=True,
            )
            await word_queue.put(end_marker)
        
        logger.debug(f"Queued {len(words)} words from in-process STT (final={event.is_final})")


    async def _switch_stt_provider(self, provider: str) -> None:
        """Switch to a different STT provider.

        Handles scenarios:
        1. Remote provider switching (google_stt <-> zipformer): stop old, start new
        2. Zipformer variant switching (zipformer_cpu <-> zipformer_gpu): update config + hard restart
        3. In-process provider switching: use STTManager.switch_provider()
        """
        current_mode = self.config_service.get("stt.mode", "remote")
        current_provider = self.config_service.get("stt.last_provider", "google_stt")

        # Map zipformer variants to base provider name for lookup
        base_provider = provider
        is_zipformer_variant = provider in ("zipformer_cpu", "zipformer_gpu")
        if is_zipformer_variant:
            base_provider = "zipformer"

        # Check if this is a remote provider (has remote_stt_launcher and is discovered)
        remote_launcher = getattr(self.service_manager, 'remote_stt_launcher', None)
        is_remote_provider = False
        if remote_launcher:
            provider_info = remote_launcher.get_provider_by_name(base_provider)
            is_remote_provider = provider_info is not None

        # Determine target mode (remote providers use remote mode, others use in_process)
        if is_remote_provider:
            target_mode = "remote"
        else:
            target_mode = "in_process"

        # Check if mode change is needed
        mode_change_needed = (current_mode != target_mode)

        if mode_change_needed:
            # Update config for mode change
            logger.info(f"STT mode change requested: {current_mode} -> {target_mode}")
            self.config_service.set("stt.mode", target_mode)
            if target_mode == "remote":
                # Store base provider (zipformer, not zipformer_cpu/gpu)
                self.config_service.set("stt.last_provider", base_provider)
                # Update zipformer GPU config if switching to a zipformer variant
                if is_zipformer_variant:
                    await self._update_zipformer_gpu_config(provider == "zipformer_gpu")
            else:
                self.config_service.set("stt.provider", provider)
            await self.config_service.save()

            # Send notification and trigger restart
            try:
                notification = {
                    'action': 'show_notification',
                    'title': 'WheelHouse: STT Mode Changed',
                    'message': f'Switching to {provider}. Restarting...',
                    'timeout': 3
                }
                self.state_manager.state_to_gui_queue.put_nowait(notification)
            except Exception:
                pass

            # Trigger restart after brief delay for notification
            await asyncio.sleep(0.5)
            await self.restart_program()
            return

        # Same mode handling
        if current_mode == "remote":
            # Remote mode: switch between remote providers
            # Map current_provider to check if it's a zipformer variant
            current_base = "zipformer" if current_provider in ("zipformer", "zipformer_cpu", "zipformer_gpu") else current_provider
            current_variant = self.state_manager._get_zipformer_variant() if current_base == "zipformer" else current_provider

            # Check if we're already on the requested provider/variant
            if provider == current_variant:
                logger.debug(f"Already using provider {provider}, no switch needed")
                return

            if remote_launcher:
                # Handle zipformer variant switching (CPU <-> GPU)
                if is_zipformer_variant and current_base == "zipformer":
                    # Switching between zipformer variants: update config + hard restart
                    logger.info(f"Switching Zipformer mode: {current_variant} -> {provider}")
                    await self._update_zipformer_gpu_config(provider == "zipformer_gpu")
                    # Show working dialog - provider will send "ready" when done
                    mode = "GPU" if provider == "zipformer_gpu" else "CPU"
                    display_name = f"Local {mode} Speech to Text"
                    try:
                        self.state_manager.state_to_gui_queue.put_nowait({
                            "action": "show_working",
                            "message": f"Loading {display_name}",
                        })
                    except Exception:
                        pass
                    # Hard restart the zipformer service
                    await self.app.websocket_manager.send_command_to_stt("hard_restart_service")
                    self.state_manager.send_state_update()
                    logger.info(f"Triggered hard restart for Zipformer mode change to {provider}")
                    return

                # Different provider: stop old, start new
                logger.info(f"Switching remote STT provider: {current_provider} -> {provider}")

                # Stop current provider (ignore failures - may already be stopped)
                await remote_launcher.stop_provider(current_base)

                # Update zipformer config if switching to a zipformer variant
                if is_zipformer_variant:
                    await self._update_zipformer_gpu_config(provider == "zipformer_gpu")

                # Start new provider (use base_provider for the actual launcher)
                if remote_launcher.start_provider(base_provider):
                    # Store base provider name in config (zipformer, not zipformer_cpu)
                    self.config_service.set("stt.last_provider", base_provider)
                    await self.config_service.save()
                    self.state_manager.send_state_update()
                    logger.info(f"Switched to remote STT provider: {provider}")
                else:
                    logger.error(f"Failed to start remote STT provider: {provider}")
        else:
            # In-process mode: use STTManager
            stt_manager = getattr(self.service_manager, 'stt_manager', None)
            if stt_manager:
                try:
                    kwargs = self.service_manager._build_stt_provider_kwargs(provider)
                    await stt_manager.switch_provider(provider, **kwargs)
                    self.state_manager.send_state_update()
                    logger.info(f"Switched STT provider to: {provider}")
                except Exception as e:
                    logger.error(f"Failed to switch STT provider to {provider}: {e}")
            else:
                # No STTManager but same mode - just update config
                self.config_service.set("stt.provider", provider)
                await self.config_service.save()
                self.state_manager.send_state_update()
                logger.info(f"Updated STT provider config to: {provider}")

    async def _switch_ai_provider(self, provider: str) -> None:
        """Select the active AI model on the thin-client coordinator.

        The tray submenu now sends a model id (the 'AI Model' menu). Switching
        is a fast, in-memory model selection on the single [ai.server] client
        -- there is no GGUF load, so no WorkingDialog IPC bracket and no
        local-model branch (get_model_by_id / switch_model / switch_provider
        are gone; design 5.4). We set the model via the coordinator and push a
        state update so the menu reflects the new selection.

        The __ai_unconfigured__ and __ai_disabled__ sentinels (state_manager
        emits them for non-selectable placeholder items) are defensively
        ignored: they are not real model ids (spec 5.4, wh-ay6h.7.2).
        """
        ai_service = getattr(self.service_manager, 'ai_service', None)
        if not ai_service:
            logger.warning("No AIService to switch -- config updated for next start")
            return

        if provider in ("__ai_unconfigured__", "__ai_disabled__"):
            logger.debug("Ignoring %s sentinel selection", provider)
            return

        await ai_service.set_model(provider)
        # A model swap can change the live list / readiness; refresh the cache
        # so the menu state is current (local kind has a live list; cloud is a
        # no-op refresh).
        await ai_service.refresh_models()
        # Persist the selection so it survives restart (finding wh-ay6h.10.5).
        self.config_service.set("ai.server.model", provider)
        await self.config_service.save()
        logger.info("AI model set to: %s", provider)

        self.state_manager.send_state_update()

    # -- Help chat handlers --

    async def _handle_help_ask(self, question: str) -> None:
        """Handle help chat question from GUI -- uses AIService public facade.

        The per-model help_capable gate is gone (design 5.4): the thin client
        targets one configured server, so capability is a server property, not
        a per-GGUF property. Readiness is checked up front (finding 1.6); on a
        non-OK ChatResult we re-probe via recheck_ready() before choosing the
        'isn't responding' wording (s7 / decision 27); a truncated OK answer
        (finish_reason == 'length') appends the 'model may be too small for
        help' hint (finding 2.4).
        """
        ai = getattr(self.service_manager, "ai_service", None)
        if not ai:
            self._send_help_error("AI service is not available.")
            return

        # Readiness gate before attempting inference (finding 1.6).
        if not ai.is_ready():
            self._send_help_error(
                "AI is not available right now. Check that the AI server is "
                "running and configured, then try again."
            )
            return

        result = await ai.help_ask(question)
        if result.ok:
            text = result.text
            if result.truncated:
                text = (
                    f"{text}\n\n[The answer was cut off -- the configured model "
                    f"may be too small for help. Try a larger model or use "
                    f"'x-ray wheelhouse help online'.]"
                )
            self._send_help_response(text)
            return

        # MODEL_NOT_FOUND means the server responded (404 on the model), so
        # a reachability re-probe would only mislead -- name the real
        # problem instead (wh-75m). Local import keeps main.py free of a
        # module-level ai.providers dependency.
        from ai.providers.openai_compat import ChatStatus

        if result.status is ChatStatus.MODEL_NOT_FOUND:
            self._send_help_error(
                "The AI server doesn't have the configured model. Check "
                "the model name in the AI settings, then try again."
            )
            return

        # A reasoning model that spent the whole budget on hidden thinking
        # also responded fine at the HTTP level -- name the real problem
        # (wh-ai-reasoning-model-empty).
        if result.exhausted_reasoning:
            self._send_help_error(
                "The AI model spent its whole answer budget on hidden "
                "reasoning and returned nothing. Configure a non-reasoning "
                "model, or raise max_response_tokens under [ai.help], "
                "then try again."
            )
            return

        # Non-OK: re-probe reachability through the coordinator before wording
        # so a server that just came back is not reported as down (s7 / d27).
        still_ready = await ai.recheck_ready()
        if not still_ready:
            self._send_help_error(
                "The AI server isn't responding. Check that it is running, "
                "then try again."
            )
        else:
            self._send_help_error(
                "Failed to get a response from the AI model. Please try again."
            )

    def _handle_help_reset(self) -> None:
        """Handle help chat reset (New Chat) from GUI."""
        ai = getattr(self.service_manager, "ai_service", None)
        if ai:
            ai.help_reset()

    def _handle_help_cancel(self) -> None:
        """Handle help chat cancel from GUI."""
        ai = getattr(self.service_manager, "ai_service", None)
        if ai:
            ai.cancel_requested = True

    def _send_help_response(self, text: str) -> None:
        """Send help response to GUI, handling queue-full gracefully."""
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "help_response", "text": text,
            })
        except Exception:
            logger.warning("Failed to send help response to GUI (queue full?)")

    def _send_help_error(self, message: str) -> None:
        """Send help error to GUI, handling queue-full gracefully."""
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "help_error", "message": message,
            })
        except Exception:
            logger.warning("Failed to send help error to GUI (queue full?)")

    # -- Terminal editor event routing --

    def _handle_input_event(self, msg: dict):
        """Handle unsolicited events from Input Process."""
        msg_type = msg.get("type")
        if msg_type == "te_event":
            self._forward_te_event_to_gui(msg)
        elif msg_type == "text_target_rejected":
            # wh-xxko1 (wh-9weum Phase 2): forward the structured rejection
            # event to the GUI as a show_rejection_toast action. The
            # schema-validation path catches a malformed payload (per
            # wh-uf54) so a sender bug cannot crash the logic loop.
            self._forward_rejection_event_to_gui(msg)
        else:
            logger.warning("Unknown event type from Input: %s", msg_type)

    def _forward_rejection_event_to_gui(self, msg: dict):
        """Forward a text_target_rejected event from Input to GUI (wh-xxko1).

        Validates the payload via ``TextTargetRejectedEvent.from_dict``
        so a malformed sender (bug or version skew) never crashes the
        logic loop. On schema error, the handler logs a warning and
        drops the event (wh-uf54).

        The forwarded GUI message uses ``show_rejection_toast`` as its
        action and carries every rendering field so the GUI can pick
        branched wording. The dictation text is NOT in this payload --
        it lives only in the input-process cache keyed by
        ``correlation_token`` (wh-7318z privacy contract).
        """
        from shared.text_target_rejection import (
            TextTargetRejectedEvent,
            TextTargetRejectedSchemaError,
        )

        try:
            event = TextTargetRejectedEvent.from_dict(msg)
        except TextTargetRejectedSchemaError as exc:
            logger.warning(
                "text_target_rejected event dropped, malformed payload: %s",
                exc,
            )
            return

        if not self.state_manager or not hasattr(
            self.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "text_target_rejected event dropped, "
                "no state_manager.state_to_gui_queue available"
            )
            return

        # wh-iycks: populate the Logic-side token -> tuple cache
        # alongside the GUI forward. The tuple carries only the
        # identifying fields the Phase 4 retry pipeline (and future
        # wh-82lnx counter / wh-bqv9c three-strikes consumers) need.
        # The dictation text is NOT stored here -- Input owns the text
        # cache. We populate the cache before posting to the GUI queue
        # so a click that races back through the queues finds the
        # token already present.
        from shared.rejection_token_cache import RejectionTuple
        try:
            self.rejection_token_cache.put(
                event.correlation_token,
                RejectionTuple(
                    process_name=event.process_name,
                    class_name=event.class_name,
                    control_type=event.control_type,
                    app_friendly_name=event.app_friendly_name,
                ),
            )
        except Exception as exc:
            logger.warning(
                "text_target_rejected: failed to populate "
                "rejection_token_cache: %s",
                exc,
            )

        gui_msg = {
            "action": "show_rejection_toast",
            "process_name": event.process_name,
            "class_name": event.class_name,
            "control_type": event.control_type,
            "reason": event.reason,
            "supported_patterns": event.supported_patterns,
            "app_friendly_name": event.app_friendly_name,
            "correlation_token": event.correlation_token,
        }
        try:
            self.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        except Exception as exc:
            logger.warning(
                "text_target_rejected event dropped, GUI queue put failed: %s",
                exc,
            )

    async def forward_click_element(self, query, trace_id: str) -> None:
        """Logic-side awaiter for a voice 'click <target>' (wh-tab7j).

        Called by ``ActionFunctions.click_element`` after it parses the
        spoken target into an ``ElementQuery`` and generates ``trace_id``.
        This method owns the cross-process round trip and every degrade
        path the bead names:

          1. Config gate. If voice clicking is disabled (operator opt-out
             or a [click] validation failure), short-circuit to a synthetic
             ``execution_failed:disabled_by_config`` notice (shown once per
             session) -- no IPC, no walk.
          2. Forward ``click_element`` to the Input process via
             ``app.send_request`` with ``timeout_s`` derived from the
             configured ``[click] response_timeout_ms`` (NOT
             WheelHouseApp.response_timeout_s). The ElementQuery and
             trace_id ride in the params dict.
          3. On ``asyncio.TimeoutError`` (no reply within the configured
             window) emit a synthetic ``execution_failed:timeout``.
          4. Parse the reply with ``ClickElementResponse.from_dict`` inside
             a try/except (ValueError, KeyError, TypeError). On failure log
             the truncated raw payload + the exception and emit a synthetic
             ``execution_failed:malformed_response``. No unhandled exception
             escapes the asyncio task.
          5. On a valid reply: populate the snapshot-summary cache (so a
             Phase 1.5 numbered-overlay click can resolve), and forward a
             ClickNoticeEvent to the GUI for every non-ok outcome.

        The notice WORDING is owned by wh-g4oma; this method only populates
        and forwards the ClickNoticeEvent payload (carrying trace_id).
        """
        from utils.trace_context import set_trace

        # The contextvar is per-task; set it so send_request stamps the
        # trace_id on the IPC envelope and Logic log lines correlate.
        set_trace(trace_id)
        spoken = getattr(query, "name", "") or ""

        if not self.click_config.enabled:
            if not self._click_disabled_notice_shown:
                self._click_disabled_notice_shown = True
                logger.info(
                    "click_element: voice clicking disabled by config "
                    "(invalid_key=%s); showing one-shot notice (trace_id=%s)",
                    self.click_config.invalid_key, trace_id,
                )
                self._forward_click_notice(
                    outcome="execution_failed",
                    reason="disabled_by_config",
                    matched_name=None,
                    matched_names=(),
                    spoken_name=spoken,
                    snapshot_id=None,
                    trace_id=trace_id,
                )
            else:
                logger.debug(
                    "click_element: disabled by config; notice already "
                    "shown this session (trace_id=%s)", trace_id,
                )
            return

        # wh-n29v.17: numbered-overlay "click N" routing. Decide whether this
        # utterance is a numeric overlay pick or a by-name click, based on the
        # CURRENT overlay state machine state. The number-extraction rule:
        # parse_number_word runs on query.name ONLY when query.role is None.
        # A spoken role keyword ("click seven button" -> name="seven",
        # role="Button") is a by-name query, not click-N, so a query carrying
        # a role is never treated as a number. Using query.name (not
        # raw_utterance) also avoids trailing-STT-punctuation misses
        # ("click 7." -> name="7"). The non-fall-through cases (snapshot-item,
        # notice, held) return BEFORE the first-use hint and send_request;
        # only BY_NAME falls through to the existing flow below.
        from services.wheelhouse.speech.number_word_parser import (
            parse_number_word,
        )
        from services.wheelhouse.speech.overlay_click_router import (
            OVERLAY_NUMBERS_CHANGED,
            RoutingDecision,
            RoutingKind,
            route_click_n,
        )
        # wh-n29v.111: the ambiguous-outcome auto-open gate below reads
        # OverlayState.CLOSED to decide whether the overlay machine is idle.
        from services.wheelhouse.click_overlay_state import OverlayState

        role = getattr(query, "role", None)
        parsed_number = (
            parse_number_word(spoken) if role is None else None
        )
        # The overlay state machine is created in __init__; read it
        # defensively so a controller assembled without it (a partially-built
        # test fixture, an early-startup path) degrades to the existing
        # by-name flow rather than raising. With no machine the routing cannot
        # be overlay-aware, so by-name is the correct, behaviour-preserving
        # fall-back.
        machine = getattr(self, "click_overlay_state", None)
        # Capture the routing-relevant machine field into a local so the
        # decision-handling branches below do not reach back into ``machine``
        # (which is Optional through the defensive getattr); the non-BY_NAME
        # branches only run when a real machine produced the decision. The
        # snapshot a notice references comes from the resolver
        # (decision.snapshot_id), not from the machine pin, so the pin is not
        # captured here (wh-n29v.19.3).
        overlay_state_label = "no_machine"
        if machine is None:
            decision = RoutingDecision(RoutingKind.BY_NAME)
        else:
            overlay_state_label = machine.state.value
            decision = route_click_n(
                state=machine.state,
                parsed_number=parsed_number,
                cache=self.click_snapshot_summary_cache,
                pinned_snapshot_id=machine.pinned_snapshot_id,
                prior_pinned_snapshot_id=machine.prior_pinned_snapshot_id,
                prior_pin_deferred=machine.prior_pin_deferred,
                visible_window_is_foreground=(
                    self._overlay_refresh_visible_window_is_foreground(machine)
                    if machine.state is OverlayState.REFRESH_IN_FLIGHT
                    else None
                ),
            )

        if decision.kind is RoutingKind.SNAPSHOT_ITEM:
            # wh-overlay-fixqueue-review.2: a "click N" inside the grace
            # window after a PROACTIVE refresh swap may have been spoken
            # against the pre-swap badges. When badge N changed identity
            # across the swap, show the "numbers just changed" notice
            # instead of silently clicking the renumbered control.
            if not self._overlay_renumber_click_safe(
                parsed_number, decision.snapshot_id,
            ):
                logger.info(
                    "click_element: overlay pick N=%s blocked -- badge "
                    "identity changed across a proactive refresh swap "
                    "(state=%s, trace_id=%s)",
                    parsed_number, overlay_state_label, trace_id,
                )
                self._forward_click_notice(
                    outcome="execution_failed",
                    reason=OVERLAY_NUMBERS_CHANGED,
                    matched_name=None,
                    matched_names=(),
                    spoken_name=spoken,
                    snapshot_id=decision.snapshot_id,
                    trace_id=trace_id,
                )
                return
            logger.info(
                "click_element: overlay pick N=%s -> snapshot_item "
                "snapshot=%r item=%r (state=%s, trace_id=%s)",
                parsed_number, decision.snapshot_id, decision.item_id,
                overlay_state_label, trace_id,
            )
            self._dispatch_snapshot_item_click(
                snapshot_id=decision.snapshot_id,
                item_id=decision.item_id,
                trace_id=trace_id,
            )
            return
        if decision.kind is RoutingKind.NOTICE:
            logger.info(
                "click_element: overlay pick N=%s -> notice reason=%r "
                "(state=%s, trace_id=%s)",
                parsed_number, decision.reason, overlay_state_label, trace_id,
            )
            # wh-n29v.18.1 / wh-n29v.19.3: the resolver is the single source of
            # truth for the snapshot a notice references. It sets
            # decision.snapshot_id to the VISIBLE snapshot the number was
            # resolved against on a resolvable-but-miss notice (in
            # refresh_in_flight with a deferred prior, that is the VISIBLE prior
            # snapshot, NOT the current not-yet-painted pin), and leaves it None
            # for the ERROR-state numbers_not_showing reject and the no-pin
            # case. Forward it verbatim. Do NOT fall back to
            # overlay_pinned_snapshot_id: in ERROR the machine deliberately
            # keeps pins populated for recovery unpinning, so the fallback would
            # attach a stale pin -- a snapshot the user never saw -- to a notice
            # whose whole meaning is that numbers are not showing (wh-n29v.19.3).
            self._forward_click_notice(
                outcome="execution_failed",
                reason=decision.reason,
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=decision.snapshot_id,
                trace_id=trace_id,
            )
            return
        if decision.kind is RoutingKind.HELD:
            logger.info(
                "click_element: overlay pick N=%s -> HELD (state=%s, "
                "trace_id=%s)", parsed_number, overlay_state_label, trace_id,
            )
            self._hold_click_n(
                number=parsed_number, spoken=spoken, trace_id=trace_id,
            )
            return
        # RoutingKind.BY_NAME falls through to the existing send_request flow.

        # wh-r3xy1: first-use discovery hint. A click command is now being
        # attempted into the foreground window. If that window belongs to a
        # Chromium-family process and the screen-reader-flag opt-in is off,
        # surface the one-shot hint pointing the user at the config knob. This
        # is a read-only side observation around the click round trip -- it
        # does not alter the click execution path. The blocking foreground
        # resolution is offloaded off the asyncio loop (wh-9f3t.60.4).
        await self._maybe_show_first_use_hint(trace_id)

        timeout_ms = self.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        try:
            raw = await self.app.send_request(
                "click_element",
                params={"query": query, "trace_id": trace_id},
                timeout_s=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.error(
                "click_element: no reply within %dms; emitting "
                "execution_failed:timeout (trace_id=%s)",
                timeout_ms, trace_id,
            )
            self._forward_click_notice(
                outcome="execution_failed",
                reason="timeout",
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- never crash the task
            # A non-timeout send failure (pickle error, shared-memory write
            # failure, queue full) is NOT a timeout; tagging it timeout would
            # mislead the user-facing notice and any consumer that branches on
            # reason (wh-9f3t.56.2). Use a distinct reason; wh-g4oma owns its
            # wording. (asyncio.CancelledError is BaseException on 3.12, so it
            # is not caught here and propagates as cancellation should.)
            logger.error(
                "click_element: send_request failed (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._forward_click_notice(
                outcome="execution_failed",
                reason="send_request_failed",
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )
            return

        from shared.click_element import (
            ClickElementResponse,
            ClickElementResponseSchemaError,
        )

        try:
            response = ClickElementResponse.from_dict(raw)
        except (ClickElementResponseSchemaError, ValueError, KeyError, TypeError) as exc:
            # Log a STRUCTURAL summary only -- never raw response values
            # (wh-9f3t.55.2). A malformed ClickElementResponse can carry
            # matched_names and snapshot_summary item names lifted from the
            # foreground UI, and a skewed Input process could place arbitrary
            # local content in any field; the main log must stay free of
            # on-screen text. We emit the payload's shape -- its type, the
            # top-level field names, each value's type, and the length of any
            # sized value -- which keeps the error diagnosable without
            # exposing values.
            if isinstance(raw, dict):
                _fields = []
                for _k in sorted(map(str, raw.keys())):
                    _v = raw.get(_k)
                    try:
                        _fields.append(f"{_k}:{type(_v).__name__}[{len(_v)}]")  # type: ignore[arg-type]
                    except TypeError:
                        _fields.append(f"{_k}:{type(_v).__name__}")
                payload_shape = "dict{" + ", ".join(_fields) + "}"
            else:
                try:
                    payload_shape = f"{type(raw).__name__}[{len(raw)}]"  # type: ignore[arg-type]
                except TypeError:
                    payload_shape = type(raw).__name__
            logger.error(
                "click_element: malformed response (trace_id=%s): %s; "
                "payload_shape=%s", trace_id, exc, payload_shape,
            )
            self._forward_click_notice(
                outcome="execution_failed",
                reason="malformed_response",
                matched_name=None,
                matched_names=(),
                spoken_name=spoken,
                snapshot_id=None,
                trace_id=trace_id,
            )
            return

        # Populate the snapshot-summary cache for the Phase 1.5 overlay
        # round trip. Keyed by snapshot_id; safe to skip when no walk
        # produced a summary (e.g. a synthetic Input-side failure).
        if response.snapshot_id and response.snapshot_summary is not None:
            try:
                self.click_snapshot_summary_cache.put(
                    response.snapshot_id, response.snapshot_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "click_element: failed to populate snapshot cache "
                    "(trace_id=%s): %s", trace_id, exc,
                )

        if response.outcome == "ok":
            logger.info(
                "click_element: ok matched=%r (trace_id=%s)",
                response.matched_name, trace_id,
            )
            return

        # wh-n29v.111: auto-open the numbered overlay on an ambiguous match,
        # restricted to the ambiguous finalists, instead of the plain notice.
        # The decision tree (v4 design "## Auto-open on ambiguous match") gates
        # on overlay_enabled_effective AND overlay_auto_open_on_ambiguous AND a
        # usable (snapshot_id, ambiguous_item_ids) AND the state machine sitting
        # in CLOSED (the machine admits auto_open only from closed; an overlay
        # already mid-session must not be hijacked by a stray ambiguous click).
        # The DECISION is evaluated INLINE -- not behind a helper method -- so a
        # MagicMock(spec=LogicController) test fixture that binds only
        # forward_click_element cannot auto-mock the gate into an always-truthy
        # short-circuit; the non-auto-open case must still reach the plain notice
        # below for any controller shape. The apply/stash work delegates to
        # ``_perform_auto_open_ambiguous`` only AFTER the gate elects to open.
        if (
            response.outcome == "ambiguous"
            and self.click_config.enabled
            and self.click_config.overlay_enabled_effective
            and self.click_config.overlay_auto_open_on_ambiguous
            and response.snapshot_id
            and response.ambiguous_item_ids
            and getattr(
                getattr(self, "click_overlay_state", None), "state", None,
            ) is OverlayState.CLOSED
            and self._perform_auto_open_ambiguous(response, spoken, trace_id)
        ):
            return

        # Non-ok outcome -> forward a ClickNoticeEvent to the GUI. The
        # response.trace_id echoes the one we generated; prefer it so a
        # log surface can correlate even if it diverged.
        self._forward_click_notice(
            outcome=response.outcome,
            reason=response.reason,
            matched_name=response.matched_name,
            matched_names=response.matched_names,
            spoken_name=spoken,
            snapshot_id=response.snapshot_id,
            trace_id=response.trace_id or trace_id,
        )

    def _perform_auto_open_ambiguous(self, response, spoken: str,
                                     trace_id: str) -> bool:
        """Apply the AUTO_OPEN OverlayEvent for an ambiguous click (wh-n29v.111).

        Reached only after ``forward_click_element``'s inline gate has confirmed
        overlay-on, auto-open-on, a usable ``(snapshot_id, ambiguous_item_ids)``,
        and the machine in ``CLOSED``. Returns ``True`` when the auto-open
        actually entered the machine (the caller suppresses today's notice),
        ``False`` when it did not (the caller falls back to the plain notice).
        It:

          1. builds the suppressed ``ClickNoticeEvent`` (the one today's path
             would have shown) so the machine can fire it on a later failure;
          2. applies an ``AUTO_OPEN`` ``OverlayEvent`` carrying that notice and
             the reuse ``snapshot_id`` -- the machine allocates a fresh
             ``(overlay_session_id, paint_generation)`` and emits the
             ``DISPATCH_BUILD(AUTO_OPEN)`` + ``ARM_TIMER`` effects;
          3. STASHES the ``item_id_filter`` (the response's
             ``ambiguous_item_ids`` as a list) keyed by the freshly-allocated
             pair, so ``_overlay_dispatch_build`` reads it back when the
             AUTO_OPEN build runs (the DISPATCH_BUILD effect carries only the
             reuse snapshot_id; the filter rides in the integration layer, not on
             the Effect).

        The state transition and the effect hand-off happen synchronously here
        (``_apply_overlay_event`` only SCHEDULES the async IPC dispatch as a
        background task), so the stash is in place before the dispatch task runs.
        Never raises: any unexpected failure logs and returns ``False`` so the
        plain notice path still fires (the user must not lose feedback).
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )
        from shared.click_notice import ClickNoticeEvent

        machine = self.click_overlay_state
        snapshot_id = response.snapshot_id
        item_ids = response.ambiguous_item_ids
        try:
            suppressed = ClickNoticeEvent(
                outcome=response.outcome,
                reason=response.reason,
                matched_name=response.matched_name,
                matched_names=tuple(response.matched_names),
                spoken_name=spoken,
                app_friendly_name="",
                snapshot_id=snapshot_id,
                trace_id=response.trace_id or trace_id,
            )
        except Exception as exc:  # noqa: BLE001 -- degrade to the plain notice
            logger.warning(
                "click_element: failed to build suppressed auto-open notice; "
                "plain notice (trace_id=%s): %s", trace_id, exc,
            )
            return False

        # Apply AUTO_OPEN (closed -> walk_in_flight). The machine allocates the
        # fresh (overlay_session_id, paint_generation) during apply; read it back
        # to key the filter stash to the exact build the effect carries.
        #
        # wh-n29v.112.1: the apply, the post-apply guard, and the stash write are
        # wrapped so the docstring's "Never raises -> plain notice still fires"
        # contract holds for this region too (the notice-build above is already
        # guarded). ``_apply_overlay_event`` commits the closed -> walk_in_flight
        # transition SYNCHRONOUSLY before its (unguarded) reconcile helpers run,
        # so a raise from a reconcile helper -- or from the stash write -- after
        # that point would otherwise leave the machine wedged in walk_in_flight
        # (every later ambiguous click then fails the CLOSED gate and the overlay
        # feature is dead until restart) AND skip the caller's plain-notice
        # fallback (silent loss of feedback). On any failure we reset the machine
        # to CLOSED (the sanctioned integration recovery, mirroring the other
        # overlay recovery paths), clear the stash, and return False so the caller
        # fires the plain notice exactly once.
        try:
            self._apply_overlay_event(
                OverlayEvent(
                    kind=OverlayEventKind.AUTO_OPEN,
                    notice=suppressed,
                    snapshot_id=snapshot_id,
                ),
                source=f"auto_open ambiguous snapshot={snapshot_id}",
            )
            if machine.state is not OverlayState.WALK_IN_FLIGHT:
                # The machine did not enter the auto-open path (a defensive guard
                # against a future machine change). Fall back to the plain notice
                # so the user still gets feedback; clear any stale stash.
                self._overlay_auto_open_filter = None
                logger.warning(
                    "click_element: AUTO_OPEN did not enter walk_in_flight "
                    "(state=%s); plain notice (trace_id=%s)",
                    machine.state.value, trace_id,
                )
                return False
            pair = (machine.overlay_session_id, machine.paint_generation)
            self._overlay_auto_open_filter = (pair, list(item_ids))
            logger.info(
                "click_element: auto-open dispatched, suppressing notice "
                "(gen=%s, filter=%d id(s), trace_id=%s)",
                pair, len(item_ids), trace_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001 -- degrade to the plain notice
            logger.warning(
                "click_element: auto-open apply/stash failed; resetting the "
                "overlay machine and falling back to the plain notice "
                "(trace_id=%s): %s", trace_id, exc,
            )
            self._overlay_auto_open_filter = None
            try:
                effects = machine.reset_to_closed()
                if effects:
                    self._perform_overlay_effects(effects, trace_id=trace_id)
            except Exception as reset_exc:  # noqa: BLE001 -- best-effort un-wedge
                logger.error(
                    "click_element: failed to reset the overlay machine after an "
                    "auto-open failure; machine may remain in %s (trace_id=%s): "
                    "%s", getattr(machine, "state", None), trace_id, reset_exc,
                )
            return False

    # ------------------------------------------------------------------
    # wh-n29v.17: numbered-overlay routing stub seams + show/hide handler.
    #
    # These are the documented STUB SEAMS the routing layer hands its
    # decisions / effects to. The effect-PERFORMING integration -- the real
    # cross-process IPC dispatch of build / paint / clear, pin / unpin, the
    # 200ms "click N" hold timer, the per-state timeout timers, and the real
    # click_snapshot_item dispatch -- is owned by the integration bead
    # wh-h9a8v2. Until that bead lands, each seam LOGS what it would do so a
    # routing test can assert the seam was reached, and the integration bead
    # replaces the body without touching the routing decision layer.
    # ------------------------------------------------------------------
    def _dispatch_snapshot_item_click(
        self, *, snapshot_id: Optional[str], item_id: Optional[str],
        trace_id: str,
    ) -> None:
        """Dispatch a numbered-overlay item click to the Input process (wh-n29v.95).

        The routing layer (voice ``click N``) or the hold timer resolved a
        numbered badge to (snapshot_id, item_id). This schedules the real
        ``click_snapshot_item`` IPC round trip as a background task so the
        synchronous caller (``forward_click_element`` after a routing decision)
        does not block on the cross-process send. ``_send_snapshot_item_click``
        owns the await, the ClickElementResponse parse, and the click-notice on
        any non-ok outcome (no notice on ok).

        POST-CLICK REFRESH GATE: the machine's CURRENT
        (overlay_session_id, paint_generation) is captured HERE, at dispatch
        time, and threaded to ``_send_snapshot_item_click`` so the success
        branch can feed CLICK_COMPLETE against the overlay the click was
        actually dispatched against. Capturing the pair at dispatch (rather
        than re-reading the machine when the ok response arrives) is what makes
        the gate correct: if the overlay is superseded (same session, newer
        generation) or torn down (new session) between dispatch and the ok
        reply, the captured pair no longer matches the live machine and the
        refresh is suppressed -- mirrors the ``_hold_click_n`` /
        ``_resolve_held_click_n`` armed-pair pattern (wh-n29v.96.3).
        """
        if not snapshot_id or not item_id:
            # wh-n29v.97.2: an unusable (snapshot_id, item_id) is an invariant
            # violation (the resolver only emits SNAPSHOT_ITEM with both set),
            # but a silent drop leaves a hands-free user with no feedback that
            # the accepted "click N" did nothing. Honour this slice's
            # never-a-silent-drop rule: surface an execution_failed notice with
            # a distinct reason and do NOT schedule an Input round trip.
            logger.warning(
                "overlay: snapshot-item click missing identity "
                "(snapshot=%r item=%r, trace_id=%s); surfacing "
                "execution_failed:invalid_snapshot_item",
                snapshot_id, item_id, trace_id,
            )
            self._forward_click_notice(
                outcome="execution_failed", reason="invalid_snapshot_item",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id or None, trace_id=trace_id,
            )
            return
        machine = getattr(self, "click_overlay_state", None)
        overlay_dispatch_pair = (
            (machine.overlay_session_id, machine.paint_generation)
            if machine is not None
            else None
        )
        self.create_task_with_error_handling(
            self._send_snapshot_item_click(
                snapshot_id=snapshot_id, item_id=item_id, trace_id=trace_id,
                overlay_dispatch_pair=overlay_dispatch_pair,
            ),
            "ClickSnapshotItem",
        )

    async def _send_snapshot_item_click(
        self, *, snapshot_id: str, item_id: str, trace_id: str,
        overlay_dispatch_pair: "Optional[tuple[int, int]]" = None,
    ) -> None:
        """Send ``click_snapshot_item`` to Input and surface the notice (wh-n29v.95).

        Mirrors the ``click_element`` awaiter's degrade paths exactly: a
        timeout, a non-timeout send failure, and a malformed reply each emit a
        synthetic ``execution_failed`` notice with a distinct reason tag rather
        than crashing the task. On a valid reply, every non-ok outcome forwards
        a ClickNoticeEvent; ``outcome="ok"`` shows NO notice. Never raises.

        POST-CLICK REFRESH: on an ``ok`` outcome this feeds an
        OverlayEvent(CLICK_COMPLETE) so a painted overlay refreshes
        (painted -> refresh_in_flight, generation bumped) instead of staying
        pinned to the now-stale pre-click UI. ``overlay_dispatch_pair`` is the
        (overlay_session_id, paint_generation) captured at click-dispatch time;
        the feed is gated on it (see ``_feed_click_complete_refresh``).
        """
        from utils.trace_context import set_trace

        set_trace(trace_id)
        timeout_ms = self.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        try:
            raw = await self.app.send_request(
                "click_snapshot_item",
                params={
                    "snapshot_id": snapshot_id,
                    "item_id": item_id,
                    "trace_id": trace_id,
                },
                timeout_s=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.error(
                "click_snapshot_item: no reply within %dms; "
                "execution_failed:timeout (trace_id=%s)", timeout_ms, trace_id,
            )
            self._forward_click_notice(
                outcome="execution_failed", reason="timeout",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- never crash the task
            # CancelledError is BaseException on 3.12, so it is not caught here.
            logger.error(
                "click_snapshot_item: send_request failed (trace_id=%s): %s",
                trace_id, exc, exc_info=True,
            )
            self._forward_click_notice(
                outcome="execution_failed", reason="send_request_failed",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return

        from shared.click_element import (
            ClickElementResponse,
            ClickElementResponseSchemaError,
        )

        try:
            response = ClickElementResponse.from_dict(raw)
        except (
            ClickElementResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.error(
                "click_snapshot_item: malformed response (trace_id=%s): %s",
                trace_id, exc,
            )
            self._forward_click_notice(
                outcome="execution_failed", reason="malformed_response",
                matched_name=None, matched_names=(), spoken_name="",
                snapshot_id=snapshot_id, trace_id=trace_id,
            )
            return

        if response.outcome == "ok":
            logger.info(
                "click_snapshot_item: ok matched=%r (trace_id=%s)",
                response.matched_name, trace_id,
            )
            self._feed_click_complete_refresh(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=trace_id,
            )
            return

        self._forward_click_notice(
            outcome=response.outcome,
            reason=response.reason,
            matched_name=response.matched_name,
            matched_names=response.matched_names,
            spoken_name="",
            snapshot_id=response.snapshot_id or snapshot_id,
            trace_id=response.trace_id or trace_id,
        )

        # wh-overlay-stale-click-refresh: a click refused because the badge no
        # longer points at a clickable control in the same place (the saved
        # position is unreadable/zero-area, drifted, scrolled off-screen, or its
        # owning popup closed) means the layout changed after the overlay was
        # painted. Re-walk and repaint so the next 'click N' resolves against
        # fresh positions; otherwise the stale snapshot stays pinned and every
        # retry fails the same way. The exact reason set lives in
        # _OVERLAY_REWALK_REFUSAL_REASONS -- a transport failure, a disabled
        # control, or a found-but-unresponsive control is excluded because a
        # re-walk would not change the outcome. Reuses the success path's
        # generation/session + PAINTED gating, so a superseded / torn-down /
        # paused overlay is a safe no-op.
        if response.reason in _OVERLAY_REWALK_REFUSAL_REASONS:
            self._feed_click_complete_refresh(
                overlay_dispatch_pair=overlay_dispatch_pair,
                trace_id=response.trace_id or trace_id,
                trigger=f"click refused ({response.reason})",
            )

    def _feed_click_complete_refresh(
        self, *, overlay_dispatch_pair: "Optional[tuple[int, int]]",
        trace_id: str,
        trigger: str = "click ok",
    ) -> None:
        """Feed CLICK_COMPLETE after an overlay item click completes (this slice).

        Called on a successful 'click N', and on a 'click N' refused for a stale
        or unreadable saved position (bounds_invalid / bounds_stale,
        wh-overlay-stale-click-refresh). Both mean the on-screen layout the
        overlay was pinned to has changed, so a fresh walk + repaint is wanted.
        ``trigger`` only labels the log lines (the success path passes the
        default).

        The state machine already implements ``painted`` + CLICK_COMPLETE ->
        ``_refresh`` (painted -> refresh_in_flight, generation bumped); nothing
        on the Logic side fed it, so after a successful 'click N' the overlay
        stayed pinned to the now-stale pre-click UI and a following 'click N'
        resolved against a stale snapshot. This feeds the event through the SAME
        ``_apply_overlay_event`` -> ``_perform_overlay_effects`` path the rest of
        the feature uses, preserving the single-``_overlay_effect_lock`` FIFO
        batch ordering.

        GENERATION/SESSION GATE: CLICK_COMPLETE is NOT a generation-bearing kind
        (the machine ignores its (overlay_session_id, paint_generation) and does
        not run the pre-table stale-generation check on it), so the gate MUST
        live here. Two conditions must both hold before feeding:

          1. The machine's CURRENT (overlay_session_id, paint_generation) still
             equals ``overlay_dispatch_pair`` -- the pair captured when the click
             was dispatched. A supersede (same session, newer generation) or a
             new session that replaced the overlay between dispatch and the ok
             reply makes the pair mismatch, so a stale click cannot drive a
             refresh on the newer overlay (mirrors ``_resolve_held_click_n``).
          2. The machine is in ``PAINTED``. A painted overlay paused via mic
             (painted -> paused) does NOT bump the generation, so the pair still
             matches; feeding CLICK_COMPLETE there would hit ``_on_paused``'s
             ``_invalid`` path and drive the machine to ``error``. Requiring
             PAINTED keeps a late/closed/paused CLICK_COMPLETE a Logic-side
             no-op rather than an error, preserving wh-n29v.95 part 6.

        Feeding inline here is safe for the lock ordering: this runs from the
        ``_send_snapshot_item_click`` awaiter (its own background task), NOT from
        inside an in-flight ``_dispatch_overlay_effects`` batch, so it does not
        re-enter the held ``_overlay_effect_lock``. ``_apply_overlay_event``
        commits the transition synchronously and SCHEDULES the refresh effects as
        a fresh, separately-locked batch, so FIFO batch ordering is preserved
        without a ``call_soon`` hop (unlike ``_overlay_dispatch_build._feed``,
        which defers precisely because it runs WHILE holding the lock).
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or overlay_dispatch_pair is None:
            return
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair != overlay_dispatch_pair:
            logger.info(
                "overlay: %s dispatched at gen=%s but machine is now "
                "gen=%s (state=%s); suppressing post-click refresh to avoid "
                "refreshing a superseded/torn-down overlay (trace_id=%s)",
                trigger, overlay_dispatch_pair, current_pair,
                machine.state.value, trace_id,
            )
            return
        if machine.state is OverlayState.REFRESH_IN_FLIGHT:
            # wh-n29v.101.1: the click resolved against the still-visible
            # pre-click overlay while a refresh for THIS generation is in flight.
            # CLICK_COMPLETE returns HELD in refresh_in_flight (a machine no-op),
            # so feeding it now would silently drop the post-click refresh: the
            # in-flight generation would then paint a pre-click snapshot with no
            # later CLICK_COMPLETE, and the next 'click N' would resolve against
            # it. Instead record a pending post-click refresh keyed on the
            # captured pair; ``_reconcile_overlay_pending_postclick_refresh``
            # replays CLICK_COMPLETE once this generation settles into PAINTED
            # (or drops it if the overlay is superseded / torn down first).
            self._overlay_pending_postclick_refresh = current_pair
            logger.info(
                "overlay: %s at matching gen=%s but machine is "
                "refresh_in_flight; deferring post-click refresh until that "
                "generation settles into painted (trace_id=%s)",
                trigger, current_pair, trace_id,
            )
            return
        if machine.state is not OverlayState.PAINTED:
            logger.info(
                "overlay: %s at matching gen=%s but machine state=%s "
                "(not painted); post-click refresh is a no-op (trace_id=%s)",
                trigger, current_pair, machine.state.value, trace_id,
            )
            return
        self._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.CLICK_COMPLETE),
            source=f"click_complete ({trigger}) gen={current_pair}",
        )

    def _reconcile_overlay_pending_postclick_refresh(self) -> None:
        """Consume or clear a deferred post-click refresh (wh-n29v.101.1).

        Called after EVERY overlay-machine apply (both ``_apply_overlay_event``
        and ``handle_overlay_command``), mirroring the sibling
        ``_reconcile_overlay_tracked_identity`` /
        ``_reconcile_overlay_keepalive_timer`` post-apply reconciles. Reads the
        machine's CURRENT state -- not a prev/next edge -- so it is idempotent
        and safe to call after any apply.

        A pending post-click refresh is the pair captured when a click ok arrived
        while the overlay was REFRESH_IN_FLIGHT (see
        ``_feed_click_complete_refresh``). It is resolved as follows:

          * CURRENT pair still equals the recorded pair AND the machine has
            settled into PAINTED -> the in-flight generation the click was
            dispatched against is now visible and is still a PRE-CLICK snapshot
            (the refresh fall-back paths return to PAINTED at the SAME generation,
            whether the refresh built a new snapshot or kept the prior one).
            Consume the pending refresh by replaying CLICK_COMPLETE so a FRESH
            post-click walk runs.
          * CURRENT pair still equals the recorded pair AND the machine is still
            settling (REFRESH_IN_FLIGHT, or PAUSED via a mid-flight mic-pause) ->
            keep waiting; that generation has not reached PAINTED yet. A later
            mic-resume back to PAINTED at the same pair consumes it then.
          * Anything else -- a supersede bumped the generation, a new session
            replaced the overlay, the session ended (closed), or the machine
            failed closed (error) -> the overlay the click was dispatched against
            is gone, so DROP the pending refresh; it must never fire on a
            different overlay.

        The consume is DEFERRED via ``loop.call_soon`` rather than applied
        inline: this runs from the tail of an ``_apply_overlay_event`` that just
        committed a paint-ack, and replaying CLICK_COMPLETE re-enters
        ``_apply_overlay_event``. Deferring keeps the re-entry off the current
        synchronous stack and preserves FIFO effect-batch ordering on the single
        Logic loop (the same call_soon idiom the build-response feed uses,
        wh-n29v.96.4). The deferred callback re-validates before applying, so an
        event arriving between scheduling and firing cannot drive a stale
        refresh. The pending pair is left SET at schedule time, NOT cleared here:
        the callback owns the consume / keep / drop decision against the live
        machine. Clearing at schedule time would lose the refresh if the machine
        moved to a keep-waiting state (a mic-pause -> PAUSED) between the schedule
        and the callback firing -- the callback would then see a non-PAINTED
        state with the pending already gone and could not keep it
        (wh-n29v.102.1). A redundant re-schedule is harmless because the callback
        re-validates and applies CLICK_COMPLETE at most once per generation
        (applying it moves the machine out of PAINTED at that pair, so a second
        callback no-ops).
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        pending = getattr(self, "_overlay_pending_postclick_refresh", None)
        if machine is None or pending is None:
            return
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair == pending:
            if machine.state is OverlayState.PAINTED:
                # Schedule the consume but leave the pending SET; the callback
                # clears it on a successful apply. Clearing here would lose the
                # refresh on a schedule/fire PAUSE interleave (wh-n29v.102.1).
                self.loop.call_soon(
                    lambda: self._apply_pending_postclick_click_complete(
                        current_pair
                    )
                )
                return
            if machine.state in (
                OverlayState.REFRESH_IN_FLIGHT, OverlayState.PAUSED,
            ):
                # Same overlay, still settling -> keep the pending refresh; it
                # consumes when this generation next reaches PAINTED.
                return
        # Superseded / new session / closed / error: the overlay the click was
        # dispatched against is gone. Drop the pending refresh so it cannot fire
        # on a different overlay.
        self._overlay_pending_postclick_refresh = None
        logger.info(
            "overlay: dropping deferred post-click refresh for gen=%s; machine "
            "moved on (now gen=%s, state=%s)",
            pending, current_pair, machine.state.value,
        )

    def _apply_pending_postclick_click_complete(
        self, pending_pair: "tuple[int, int]",
    ) -> None:
        """Deferred replay of a pending post-click CLICK_COMPLETE (wh-n29v.101.1).

        Scheduled by ``_reconcile_overlay_pending_postclick_refresh`` when the
        recorded generation reached PAINTED. The pending pair is NOT cleared at
        schedule time, so this callback owns the consume / keep / drop decision
        against the LIVE machine (wh-n29v.102.1). An event may have been processed
        between the call_soon scheduling and this firing:

          * A newer pending refresh replaced ours (the field no longer equals
            ``pending_pair``) -> do nothing; the newer pending owns the field and
            its own callback will handle it.
          * Still PAINTED at ``pending_pair`` -> consume: clear the pending and
            replay CLICK_COMPLETE. ``_apply_overlay_event`` mints its own trace id
            for the resulting refresh, exactly as the inline PAINTED feed in
            ``_feed_click_complete_refresh`` does.
          * Still at ``pending_pair`` but in a keep-waiting state
            (REFRESH_IN_FLIGHT or PAUSED -- e.g. a mic-pause landed between
            schedule and fire) -> leave the pending SET; the next apply that
            reaches PAINTED at this pair reschedules and consumes it. This is what
            stops a paused interlude from losing the refresh.
          * Otherwise (the generation was superseded, a new session replaced the
            overlay, the session ended, or the machine failed closed) -> drop the
            pending; the overlay the click was dispatched against is gone.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if getattr(self, "_overlay_pending_postclick_refresh", None) != pending_pair:
            # A newer pending (or a clear) replaced ours after this callback was
            # scheduled; it is not ours to act on.
            return
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair == pending_pair:
            if machine.state is OverlayState.PAINTED:
                self._overlay_pending_postclick_refresh = None
                self._apply_overlay_event(
                    OverlayEvent(kind=OverlayEventKind.CLICK_COMPLETE),
                    source=f"click_complete_deferred gen={current_pair}",
                )
                return
            if machine.state in (
                OverlayState.REFRESH_IN_FLIGHT, OverlayState.PAUSED,
            ):
                # Still settling at the same overlay -> keep the pending; the
                # next reach-PAINTED reschedules it.
                logger.info(
                    "overlay: deferred post-click refresh for gen=%s still "
                    "settling (state=%s); keeping it pending",
                    pending_pair, machine.state.value,
                )
                return
        # Superseded / new session / closed / error -> drop.
        self._overlay_pending_postclick_refresh = None
        logger.info(
            "overlay: deferred post-click refresh for gen=%s dropped; machine "
            "is now gen=%s state=%s",
            pending_pair, current_pair, machine.state.value,
        )

    def _hold_click_n(
        self, *, number: Optional[int], spoken: str, trace_id: str,
    ) -> None:
        """Hold a 'click N' that arrived during a transition, up to 200ms (wh-n29v.95).

        The v4 "queue or drop" rule: a numeric ``click N`` that lands while the
        machine is in a transitional state (walk_in_flight / paint_in_flight /
        paused) cannot resolve yet. Arm a single 200ms hold timer; when it
        fires, RE-READ the machine and re-route through the same resolver
        ``forward_click_element`` used. If the overlay has reached a resolvable
        state, dispatch the click; otherwise emit the "numbers did not paint
        yet" notice. NEVER a silent drop (criterion 1, wh-n29v.18.2). Only one
        hold is live at a time; a second held click cancels and re-arms it.

        wh-n29v.96.3: stamp the machine's CURRENT (overlay_session_id,
        paint_generation) onto the held click so ``_resolve_held_click_n`` can
        reject it if a supersede or new session reached a resolvable state during
        the hold -- otherwise the held N would resolve against a snapshot the
        user never saw and dispatch a real click against the WRONG control.
        """
        existing = getattr(self, "_overlay_hold_timer", None)
        if existing is not None:
            existing.cancel()
        machine = self.click_overlay_state
        armed_pair = (machine.overlay_session_id, machine.paint_generation)
        delay_s = 0.2
        self._overlay_hold_timer = self.loop.call_later(
            delay_s,
            lambda: self._resolve_held_click_n(
                number=number, spoken=spoken, trace_id=trace_id,
                armed_pair=armed_pair,
            ),
        )

    def _resolve_held_click_n(
        self, *, number: Optional[int], spoken: str, trace_id: str,
        armed_pair: "tuple[int, int]",
    ) -> None:
        """Hold-timer callback: re-read the machine and resolve-or-notice (wh-n29v.95).

        Runs on the Logic loop. Re-routes the held number through
        ``route_click_n`` against the CURRENT machine state. A SNAPSHOT_ITEM
        decision dispatches the click; any other decision (still HELD, NOTICE,
        or BY_NAME) means the overlay never reached a resolvable painted state
        within the hold window, so it surfaces the "numbers did not paint yet"
        notice rather than dropping the click silently.

        wh-n29v.96.3 generation gate: before dispatching SNAPSHOT_ITEM, require
        the machine's CURRENT (overlay_session_id, paint_generation) to still
        equal ``armed_pair`` -- the pair captured when the hold was armed. A
        supersede (same session, new generation) or a new session that reached a
        resolvable state within the 200ms hold would otherwise make the held N
        resolve against a DIFFERENT snapshot the user never saw. On a mismatch,
        surface the numbers_not_showing notice instead of clicking the wrong
        control.
        """
        self._overlay_hold_timer = None
        from services.wheelhouse.speech.overlay_click_router import (
            RoutingKind,
            route_click_n,
        )
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = self.click_overlay_state
        current_pair = (machine.overlay_session_id, machine.paint_generation)
        if current_pair != armed_pair:
            # The overlay was superseded / a new session started during the hold.
            # Resolving N now would target a snapshot the user never saw, so do
            # NOT dispatch a click -- surface the numbers-not-showing notice.
            logger.info(
                "overlay: held click N=%s armed at gen=%s but machine is now "
                "gen=%s (state=%s); rejecting to avoid a wrong-overlay click "
                "(trace_id=%s)",
                number, armed_pair, current_pair, machine.state.value, trace_id,
            )
            self._forward_click_notice(
                outcome="execution_failed", reason="numbers_not_showing",
                matched_name=None, matched_names=(), spoken_name=spoken,
                snapshot_id=None, trace_id=trace_id,
            )
            return
        decision = route_click_n(
            state=machine.state,
            parsed_number=number,
            cache=self.click_snapshot_summary_cache,
            pinned_snapshot_id=machine.pinned_snapshot_id,
            prior_pinned_snapshot_id=machine.prior_pinned_snapshot_id,
            prior_pin_deferred=machine.prior_pin_deferred,
            visible_window_is_foreground=(
                self._overlay_refresh_visible_window_is_foreground(machine)
                if machine.state is OverlayState.REFRESH_IN_FLIGHT
                else None
            ),
        )
        if decision.kind is RoutingKind.SNAPSHOT_ITEM:
            logger.info(
                "overlay: held click N=%s resolved after hold -> "
                "snapshot_item snapshot=%r item=%r (state=%s, trace_id=%s)",
                number, decision.snapshot_id, decision.item_id,
                machine.state.value, trace_id,
            )
            self._dispatch_snapshot_item_click(
                snapshot_id=decision.snapshot_id,
                item_id=decision.item_id,
                trace_id=trace_id,
            )
            return
        if decision.kind is RoutingKind.NOTICE:
            logger.info(
                "overlay: held click N=%s -> notice reason=%r after hold "
                "(state=%s, trace_id=%s)",
                number, decision.reason, machine.state.value, trace_id,
            )
            self._forward_click_notice(
                outcome="execution_failed", reason=decision.reason,
                matched_name=None, matched_names=(), spoken_name=spoken,
                snapshot_id=decision.snapshot_id, trace_id=trace_id,
            )
            return
        # Still HELD (overlay never painted within the hold), or BY_NAME (the
        # overlay tore down to closed during the hold). Either way the numbers
        # are not showing for this pick: surface the notice, never a silent drop
        # (criterion 1, wh-n29v.18.2).
        logger.info(
            "overlay: held click N=%s did not become resolvable within the "
            "hold (decision=%s, state=%s); firing numbers-not-showing notice "
            "(trace_id=%s)",
            number, decision.kind.value, machine.state.value, trace_id,
        )
        self._forward_click_notice(
            outcome="execution_failed", reason="numbers_not_showing",
            matched_name=None, matched_names=(), spoken_name=spoken,
            snapshot_id=None, trace_id=trace_id,
        )

    async def handle_overlay_command(
        self, command: str, trace_id: str,
    ) -> None:
        """Apply a 'show numbers' / 'hide numbers' voice command (wh-n29v.17).

        Delegated to by ``ActionFunctions.show_overlay_command`` /
        ``hide_overlay_command``. Gated on BOTH ``click_config.enabled`` AND
        ``click_config.overlay_enabled_effective``: the overlay runs only when
        voice clicking is on AND the overlay itself is effectively enabled. A
        bad overlay key disables only the overlay (``overlay_enabled_effective``
        False) while by-name click stays operative; a valid ``enabled=false``
        master opt-out leaves ``overlay_enabled_effective`` True, so the
        ``enabled`` term is still required. This matches the Input-side overlay
        walk gate (``ui_action_handler._get_overlay_walk_finder``), which
        requires the ``enabled``-gated finder AND ``overlay_enabled_effective``,
        so the Logic and Input processes agree on whether the overlay is on.
        When the overlay is not active the commands are a no-op (no machine
        transition, no effects). Applies
        ``SHOW_NUMBERS`` or ``HIDE_NUMBERS`` to ``self.click_overlay_state`` and
        hands the returned ordered effects to the ``_perform_overlay_effects``
        stub seam. The state TRANSITION happens here (the machine is pure and
        single-thread-owned by this Logic loop); PERFORMING the effects is the
        integration bead's job.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            logger.debug(
                "overlay: %r ignored, numbered overlay not active "
                "(enabled=%s, overlay_invalid_key=%s, trace_id=%s)",
                command, self.click_config.enabled,
                self.click_config.overlay_invalid_key, trace_id,
            )
            return

        if command == "show":
            kind = OverlayEventKind.SHOW_NUMBERS
        elif command == "hide":
            kind = OverlayEventKind.HIDE_NUMBERS
        else:
            logger.warning(
                "overlay: unknown command %r ignored (trace_id=%s)",
                command, trace_id,
            )
            return

        result = self.click_overlay_state.apply(OverlayEvent(kind))
        logger.info(
            "overlay: %s -> state=%s outcome=%s (%d effect(s), trace_id=%s)",
            command, self.click_overlay_state.state.value,
            result.outcome.value, len(result.effects), trace_id,
        )
        if result.effects:
            self._perform_overlay_effects(result.effects, trace_id=trace_id)
        # wh-n29v.95 part 5: clear the tracked identity when the command (e.g.
        # hide) drove the machine to closed. hide_numbers does NOT go through
        # _apply_overlay_event, so reconcile here too.
        self._reconcile_overlay_tracked_identity()
        # wh-n29v.96.2: arm/cancel the periodic visible-snapshot keepalive to
        # match the resulting PAINTED/PAUSED-ness.
        self._reconcile_overlay_keepalive_timer()
        # wh-n29v.101.1: clear any deferred post-click refresh when a show/hide
        # command supersedes or tears down the overlay it was recorded against.
        self._reconcile_overlay_pending_postclick_refresh()
        # wh-n29v.114.1: hide_numbers can drive the machine to closed while an
        # auto-open item_id_filter is still stashed; this path bypasses
        # _apply_overlay_event, so clear the stash here too (shared helper).
        self._reconcile_overlay_auto_open_filter()
        # wh-overlay-nested-dupes.1.4: hide_numbers can drive the machine to
        # closed while a settle re-fire from a coalesced foreground/menu event
        # is still pending; cancel it here too, or an immediate 'show numbers'
        # lets the stale timer restart the fresh session's build.
        self._reconcile_overlay_settle_refire()
        # wh-overlay-fixqueue-review.1/.2: hide_numbers ends the session, so
        # the proactive-refresh back-off and renumber guard reset here too.
        self._reconcile_overlay_browser_refresh_reset()

    async def _handle_overlay_state_changed(self, command) -> None:
        """Consume the GUI's ``overlay_state_changed`` paint-ack (wh-n29v.67).

        After the GUI applies / fails / clears a numbered-overlay paint, it
        emits ``overlay_state_changed { state, overlay_session_id,
        paint_generation, monitor_ids, snapshot_id }`` on the
        commands_to_logic_queue. This is the Logic-side consumer that feeds the
        paint-acknowledgement into the held ``ClickOverlayStateMachine`` instead
        of letting it fall through as an unknown GUI action.

          1. Validate the payload via :func:`safe_parse`. A malformed payload
             (``OverlayStateChangedEventSchemaError`` -> ``ValueError``) is
             logged and dropped, per wh-uf54, so a version-skewed sender cannot
             crash the GUI command listener.
          2. Early-return when the numbered overlay is not active -- gated on
             BOTH ``click_config.enabled`` AND
             ``click_config.overlay_enabled_effective``, matching the focus-hook
             handlers (``_on_overlay_foreground_change`` /
             ``_on_overlay_focused_hwnd_destroyed``) and the
             ``handle_overlay_command`` / Input-side overlay-walk gate so the
             Logic and Input processes agree on whether the overlay is on.
          3. Map the wire ``state`` string to the machine's ``PaintAckState``
             (the schema's closed set and ``PaintAckState`` have identical
             members, so a parsed event always maps cleanly).
          4. Build a ``PAINT_ACK`` ``OverlayEvent`` carrying the wire's
             ``overlay_session_id`` / ``paint_generation`` / ``snapshot_id`` and
             apply it THROUGH ``_apply_overlay_event`` so the effect hand-off
             (``_perform_overlay_effects``), the session-end debouncer reset, and
             the transient-destroy-hook reconciliation are reused, not
             duplicated.

        The stale-generation drop is the machine's OWN concern: ``apply`` rejects
        a ``PAINT_ACK`` whose ``(overlay_session_id, paint_generation)`` does not
        equal the active pair as ``STALE_GENERATION`` (no state change, no
        effects) BEFORE the transition table. This handler copies the wire pair
        onto the event and does NOT re-implement a generation check.
        """
        from services.wheelhouse.shared.ipc_schema_validation import safe_parse
        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            PaintAckState,
        )

        ev = safe_parse(
            OverlayStateChangedEvent.from_dict,
            command,
            log_label="overlay_state_changed",
        )
        if ev is None:
            return  # already logged

        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            logger.debug(
                "overlay: overlay_state_changed %r ignored, numbered overlay "
                "not active (enabled=%s, overlay_invalid_key=%s)",
                ev.state, self.click_config.enabled,
                self.click_config.overlay_invalid_key,
            )
            return

        # Degrade, do not die. This handler runs as a
        # create_task_with_error_handling background task whose done-callback
        # (_handle_task_completion) calls request_shutdown() on ANY uncaught
        # exception. A paint-ack must never be able to restart the whole Logic
        # process, so wrap the post-parse body in the same degrade-don't-die
        # posture _handle_snapshot_item_clicked uses (wh-9f3t.69.3): an
        # unexpected error here logs and drops the ack rather than escalating to
        # a process-wide shutdown. safe_parse above already handles the
        # malformed-payload (ValueError) path; this guard covers the two
        # remaining unprotected operations -- the PaintAckState mapping (a future
        # schema/enum drift would surface as a ValueError, since the two enums
        # live in separate files) and the _apply_overlay_event hand-off, whose
        # effects seam becomes real cross-process IPC in the integration slice
        # (wh-uf54 / wh-n29v.68.1).
        try:
            # The schema's closed _ALLOWED_STATE set
            # ('painted'/'failed'/'cleared') mirrors PaintAckState
            # member-for-member, so a parsed event always maps to a valid
            # PaintAckState. PaintAckState(ev.state) does the lookup by value.
            paint_state = PaintAckState(ev.state)

            # ev.monitor_ids is intentionally NOT forwarded onto the
            # OverlayEvent (wh-n29v.70.1). It reports which monitors the GUI
            # actually painted, but the machine targets monitors from its own
            # dispatch (the paint/clear effects it emits), not from the ack, and
            # clear is idempotent -- clearing a monitor with no overlay is a safe
            # no-op -- so the machine never needs the ack's monitor set to route
            # effects. monitor_ids stays available here at the Logic boundary for
            # diagnostic logging if the integration bead (wh-overlay-click-
            # integration) later needs per-monitor reconciliation; it would add
            # the field to OverlayEvent/Effect at that point. Carrying an unused
            # field through the machine now would be its own contract hazard.
            self._apply_overlay_event(
                OverlayEvent(
                    kind=OverlayEventKind.PAINT_ACK,
                    overlay_session_id=ev.overlay_session_id,
                    paint_generation=ev.paint_generation,
                    snapshot_id=ev.snapshot_id,
                    paint_state=paint_state,
                ),
                source=(
                    f"overlay_state_changed state={ev.state} "
                    f"gen=({ev.overlay_session_id},{ev.paint_generation})"
                ),
            )
        except Exception:
            logger.exception(
                "overlay: overlay_state_changed handler dropped an "
                "unexpected error (state=%r, gen=(%s,%s)); degrading instead "
                "of escalating to a Logic shutdown.",
                ev.state, ev.overlay_session_id, ev.paint_generation,
            )
            return

    def _perform_overlay_effects(self, effects, *, trace_id: str) -> None:
        """Perform the overlay effects the pure machine returned (wh-n29v.95).

        ``ClickOverlayStateMachine.apply`` returns the side effects as data
        (DISPATCH_BUILD / PAINT / CLEAR, PIN / UNPIN_SNAPSHOT, FIRE_NOTICE,
        ARM / CANCEL_TIMER). This is the synchronous entry the machine-feed
        paths call (``handle_overlay_command``, ``_apply_overlay_event``); it
        SCHEDULES the real cross-process dispatch as a background task rather
        than blocking the caller on the awaitable IPC.

        CONCURRENCY CONTRACT (wh-n29v.70.2): the real dispatch
        (``_dispatch_overlay_effects``) is async and acquires
        ``_overlay_effect_lock`` for the WHOLE batch, so effects ship in
        machine-return order even when multiple in-flight tasks (paint-acks,
        the build-response feed, the timer feed) call this concurrently.
        ``machine.apply`` already committed the state transition synchronously
        BEFORE this runs, so machine state is consistent; the lock guarantees
        a not-yet-completed clear from one batch is not reordered against a
        paint/clear from a later batch. asyncio.Lock is FIFO, so batches
        dispatch in the order they were scheduled on this single Logic loop.
        """
        # wh-pin-snapshot-contract-break-detection: the pure machine records
        # an over-pin contract break (a pin outside the legitimate
        # deferred-refresh two-pin window) instead of logging; surface it
        # here, the funnel every apply site's effects flow through. Checked
        # before the empty-return: a break implies a PIN effect was emitted,
        # so effects are non-empty whenever one is pending, but the ordering
        # keeps that coupling explicit rather than incidental.
        machine = getattr(self, "click_overlay_state", None)
        if machine is not None:
            break_msg = machine.consume_pin_contract_break()
            if break_msg:
                logger.warning(
                    "overlay: PIN CONTRACT BREAK -- %s (trace_id=%s). "
                    "A lost unpin or racing double-pin; the Input store may "
                    "hold a leaked pinned snapshot until TTL.",
                    break_msg, trace_id,
                )
        if not effects:
            return
        self.create_task_with_error_handling(
            self._dispatch_overlay_effects(tuple(effects), trace_id=trace_id),
            "OverlayEffects",
        )

    async def _dispatch_overlay_effects(self, effects, *, trace_id: str) -> None:
        """Async, ordering-safe dispatch of one overlay effect batch (wh-n29v.95).

        Acquires ``_overlay_effect_lock`` for the whole batch so the per-effect
        cross-process sends ship in machine-return order under concurrency. A
        DISPATCH_BUILD awaits the Input build response, populates the summary
        cache, and feeds BUILD_RESPONSE back through ``_apply_overlay_event`` so
        the machine advances. Each effect's IO is wrapped in degrade-don't-die:
        an unexpected failure on one effect logs and the batch continues, so one
        bad send cannot abort the rest of an ordered batch.
        """
        from services.wheelhouse.click_overlay_state import EffectKind

        lock = getattr(self, "_overlay_effect_lock", None)
        if lock is None:
            lock = self._overlay_effect_lock = asyncio.Lock()
        async with lock:
            for effect in effects:
                try:
                    if effect.kind is EffectKind.DISPATCH_BUILD:
                        await self._overlay_dispatch_build(effect, trace_id)
                    elif effect.kind is EffectKind.DISPATCH_PAINT:
                        await self._overlay_dispatch_paint(effect, trace_id)
                    elif effect.kind is EffectKind.DISPATCH_CLEAR:
                        await self._overlay_dispatch_clear_one(effect, trace_id)
                    elif effect.kind is EffectKind.PIN_SNAPSHOT:
                        await self._overlay_send_pin(effect, trace_id)
                    elif effect.kind is EffectKind.UNPIN_SNAPSHOT:
                        await self._overlay_send_unpin(effect, trace_id)
                    elif effect.kind is EffectKind.FIRE_NOTICE:
                        self._overlay_fire_notice(effect, trace_id)
                    elif effect.kind is EffectKind.ARM_TIMER:
                        self._overlay_arm_timer(effect, trace_id)
                    elif effect.kind is EffectKind.CANCEL_TIMER:
                        self._overlay_cancel_timer()
                except Exception:  # noqa: BLE001 -- degrade, do not abort batch
                    logger.exception(
                        "overlay: effect %s gen=(%s,%s) failed; continuing the "
                        "batch (trace_id=%s)",
                        effect.kind.value, effect.overlay_session_id,
                        effect.paint_generation, trace_id,
                    )

    def _take_auto_open_filter(
        self, sid: int, gen: int,
    ) -> "list[str] | None":
        """Pop the stashed auto-open item_id_filter for ``(sid, gen)`` (wh-n29v.111).

        ``forward_click_element`` stashes the ambiguous-finalist filter keyed by
        the auto-open ``(overlay_session_id, paint_generation)`` because the
        DISPATCH_BUILD Effect carries only the reuse snapshot_id. This reads it
        back when the AUTO_OPEN build dispatches, consuming it (single-slot, one
        auto-open in flight). Returns the filter list when the stashed pair
        matches ``(sid, gen)``, else ``None`` (no stash, or a stale/mismatched
        pair) so the build degrades to an unfiltered re-paint rather than
        applying a filter from a different overlay. Clears the slot only when it
        is the one being consumed, so a concurrent unrelated stash (which the
        single-auto-open-in-flight contract forbids in practice) is not dropped.
        """
        # Read defensively: a controller assembled via object.__new__ in a test
        # fixture may not have run __init__, so the slot may be absent. Treat an
        # absent slot the same as an empty one (no filter -> unfiltered re-paint).
        stash = getattr(self, "_overlay_auto_open_filter", None)
        if stash is None:
            return None
        pair, item_ids = stash
        if pair != (sid, gen):
            return None
        self._overlay_auto_open_filter = None
        return item_ids

    async def _overlay_dispatch_build(self, effect, trace_id: str) -> None:
        """Dispatch a build (start_overlay_walk / show_numbered_overlay) (wh-n29v.95).

        AUTO_OPEN reuses the existing click snapshot via ``show_numbered_overlay``;
        every other BuildReason is a fresh walk via ``start_overlay_walk``. The
        response echoes the (overlay_session_id, paint_generation) pair; on a
        usable response the summary cache is populated and a BUILD_RESPONSE
        OverlayEvent (build_ok, snapshot_id, echoed pair) is fed back through
        ``_apply_overlay_event`` so the machine advances. A timeout / send
        failure / malformed reply feeds a build_ok=False BUILD_RESPONSE so the
        machine takes its in-flight-failure path (fire pending/standalone
        notice -> closed) instead of stalling in walk_in_flight forever.
        """
        from utils.trace_context import set_trace
        from services.wheelhouse.click_overlay_state import (
            BuildReason,
            OverlayEvent,
            OverlayEventKind,
        )

        set_trace(trace_id)
        sid = effect.overlay_session_id
        gen = effect.paint_generation
        timeout_ms = self.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)

        if effect.build_reason is BuildReason.AUTO_OPEN:
            action = "show_numbered_overlay"
            # wh-n29v.96.1: the reuse snapshot id travels ON the effect. AUTO_OPEN
            # fires from CLOSED before the machine pins anything, so reading
            # self.click_overlay_state.pinned_snapshot_id here would send "" and
            # lose the reuse target. Read effect.snapshot_id instead.
            #
            # wh-n29v.111: the auto-open item_id_filter (the ambiguous finalists)
            # is NOT on the Effect -- it was stashed by forward_click_element
            # keyed by this build's (overlay_session_id, paint_generation). Pop it
            # for THIS pair so the Input handler restricts the painted set to the
            # finalists and renumbers them 1..K. A pair mismatch (a superseding
            # build that re-used AUTO_OPEN, which the design never does, or a
            # stale stash) yields ``None`` -> an unfiltered re-paint, the safe
            # degrade. The stash is single-slot (one auto-open in flight) and is
            # consumed exactly once here.
            item_id_filter = self._take_auto_open_filter(sid, gen)
            params: dict = {
                "snapshot_id": effect.snapshot_id or "",
                "item_id_filter": item_id_filter,
                "overlay_session_id": sid,
                "paint_generation": gen,
                "trace_id": trace_id,
            }
            parser = self._parse_show_numbered_overlay_response
        else:
            action = "start_overlay_walk"
            params = {
                "scope": "focused_window",
                "overlay_session_id": sid,
                "paint_generation": gen,
                "trace_id": trace_id,
            }
            parser = self._parse_start_overlay_walk_response

        # wh-n29v.117: emit the floating-button "walking" progress cue. This
        # method is the single funnel every walk-start passes through, so
        # active:True here covers fresh/refresh walks (start_overlay_walk) and
        # auto-open (show_numbered_overlay). It is a plain-dict side-channel
        # notification on the existing GUI state queue -- NOT a new EffectKind
        # and NOT a shared/ schema -- so the overlay state-machine contract is
        # untouched and the GUI consumer is defensive. The terminating
        # active:False rides the _feed closure below on the build-FAILURE paths
        # only (timeout, send failure, malformed, generation mismatch); on the
        # build-SUCCESS path the GUI clears the cue when paint_overlay arrives,
        # and the GUI fallback timer is the last-resort safety net (wh-n29v.119.2).
        #
        # wh-n29v.118 / wh-n29v.120.1: carry the GUI fallback bound as
        # walk_timeout_ms. On the SUCCESS path the cue must survive until
        # paint_overlay is enqueued, and paint is enqueued only AFTER BOTH the
        # walk send_request (bounded by response_timeout_ms) AND the PIN_SNAPSHOT
        # ack await in _overlay_send_pin (also bounded by response_timeout_ms)
        # complete -- the PIN_SNAPSHOT effect runs before DISPATCH_PAINT in the
        # same FIFO-locked batch. So the fallback must outlast walk + pin, i.e.
        # 2 * response_timeout_ms, not the walk alone; a fallback sized for the
        # walk alone would clear the dot during a slow/hung pin before the
        # numbers paint. Carry 2 * timeout_ms; the GUI adds its own buffer on top
        # and clamps to the Qt timer range (wh-n29v.119.1) so the doubled value
        # cannot overflow even on an unbounded response_timeout_ms.
        self._put_overlay_walk_cue(
            True, trace_id=trace_id, walk_timeout_ms=timeout_ms * 2,
        )

        def _feed(*, build_ok: bool, snapshot_id) -> None:
            # wh-n29v.117 / wh-n29v.119.2: clear the walking cue immediately
            # ONLY on the build-FAILURE paths (timeout, send failure, malformed
            # reply, generation mismatch). Those paths enqueue no paint_overlay,
            # so nothing else would clear the cue before the GUI fallback timer.
            # On the build-SUCCESS path do NOT clear here: the success
            # transition schedules PIN_SNAPSHOT then DISPATCH_PAINT, and
            # _overlay_send_pin AWAITS the pin ack (up to response_timeout_ms)
            # before paint_overlay is enqueued. Clearing the cue here would drop
            # the dot during that window, so the user would see neither the cue
            # nor the numbers -- defeating the latency-budget affordance exactly
            # when latency is worst. The GUI clears the cue as a backstop when
            # paint_overlay (or clear_overlay) arrives, and the fallback timer
            # covers the rare success-without-paint (summary cache miss).
            if not build_ok:
                self._put_overlay_walk_cue(False, trace_id=trace_id)
            # wh-n29v.96.4: DEFER the build-response transition via call_soon so
            # the in-flight build batch fully drains its remaining effects --
            # crucially the walk batch's trailing ARM_TIMER(WALK) -- and RELEASES
            # ``_overlay_effect_lock`` BEFORE the build-response transition
            # (-> paint_in_flight) schedules its own effects. Feeding inline here
            # would commit the paint transition while the WALK timer arm is still
            # pending in the same batch, briefly arming a stale WALK-duration
            # timer at the still-current generation. The call_soon hop preserves
            # FIFO ordering on the single Logic loop without holding the lock
            # across the transition.
            event = OverlayEvent(
                kind=OverlayEventKind.BUILD_RESPONSE,
                overlay_session_id=sid,
                paint_generation=gen,
                snapshot_id=snapshot_id,
                build_ok=build_ok,
            )
            source = f"build_response action={action} gen=({sid},{gen})"
            self.loop.call_soon(
                lambda: self._apply_overlay_event(event, source=source)
            )

        try:
            raw = await self.app.send_request(
                action, params=params, timeout_s=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.error(
                "overlay: %s no reply within %dms; feeding build_ok=False "
                "(gen=(%s,%s), trace_id=%s)",
                action, timeout_ms, sid, gen, trace_id,
            )
            _feed(build_ok=False, snapshot_id=None)
            return
        except Exception as exc:  # noqa: BLE001 -- CancelledError propagates
            logger.error(
                "overlay: %s send_request failed (gen=(%s,%s), trace_id=%s): "
                "%s", action, sid, gen, trace_id, exc, exc_info=True,
            )
            _feed(build_ok=False, snapshot_id=None)
            return

        parsed = parser(raw, trace_id=trace_id)
        if parsed is None:
            # Malformed payload already logged by the parser; treat as a build
            # failure so the machine recovers rather than stalling.
            _feed(build_ok=False, snapshot_id=None)
            return

        build_ok, snapshot_id, summary, echoed_sid, echoed_gen = parsed
        # wh-n29v.97.3: the response echoes (overlay_session_id,
        # paint_generation) "for the generation/supersession check" (v4 design
        # line 186: "Logic compares the echoed generation against the current
        # one ... drops the response and does NOT paint"). A response whose
        # echoed pair disagrees with the request pair is not trustworthy -- an
        # Input-side generation bug or a skewed/misrouted payload -- so treat it
        # as a build failure (recover non-destructively; do NOT paint or cache
        # it) instead of restamping it with the request pair and letting it
        # through the machine's stale-generation gate. The request pair stays
        # authoritative for the BUILD_RESPONSE feed; the echo is the cross-check.
        if (echoed_sid, echoed_gen) != (sid, gen):
            logger.error(
                "overlay: %s response echoed generation (%s,%s) != request "
                "(%s,%s); treating as build failure (trace_id=%s)",
                action, echoed_sid, echoed_gen, sid, gen, trace_id,
            )
            _feed(build_ok=False, snapshot_id=None)
            return

        if build_ok and snapshot_id and summary is not None:
            try:
                self.click_snapshot_summary_cache.put(snapshot_id, summary)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "overlay: failed to populate snapshot cache for %r "
                    "(trace_id=%s): %s", snapshot_id, trace_id, exc,
                )
        _feed(build_ok=build_ok, snapshot_id=snapshot_id if build_ok else None)

    def _parse_start_overlay_walk_response(self, raw, *, trace_id: str):
        """Parse a StartOverlayWalkResponse; return (build_ok, snapshot_id, summary)
        or None on a malformed payload (wh-uf54 degrade)."""
        from services.wheelhouse.shared.start_overlay_walk import (
            StartOverlayWalkResponse,
            StartOverlayWalkResponseSchemaError,
        )

        try:
            resp = StartOverlayWalkResponse.from_dict(raw)
        except (
            StartOverlayWalkResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.error(
                "overlay: malformed start_overlay_walk response "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return None
        return (
            resp.outcome == "ok", resp.snapshot_id, resp.snapshot_summary,
            resp.overlay_session_id, resp.paint_generation,
        )

    def _parse_show_numbered_overlay_response(self, raw, *, trace_id: str):
        """Parse a ShowNumberedOverlayResponse; return (build_ok, snapshot_id,
        summary) or None on a malformed payload (wh-uf54 degrade)."""
        from services.wheelhouse.shared.show_numbered_overlay import (
            ShowNumberedOverlayResponse,
            ShowNumberedOverlayResponseSchemaError,
        )

        try:
            resp = ShowNumberedOverlayResponse.from_dict(raw)
        except (
            ShowNumberedOverlayResponseSchemaError, ValueError, KeyError,
            TypeError,
        ) as exc:
            logger.error(
                "overlay: malformed show_numbered_overlay response "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return None
        return (
            resp.outcome == "ok", resp.snapshot_id, resp.snapshot_summary,
            resp.overlay_session_id, resp.paint_generation,
        )

    async def _overlay_dispatch_paint(self, effect, trace_id: str) -> None:
        """Put a paint_overlay action (flattened summary) on the GUI queue (wh-n29v.95).

        Looks up the summary for ``effect.snapshot_id`` in the Logic cache to
        build the PaintOverlayEvent. PART 4 keepalive: re-put the summary on
        access so the actively-painted snapshot does not age out of the resolver
        cache while it is on screen (criterion 4). A cache miss (the summary was
        evicted) logs and skips the paint -- there is nothing to draw.
        """
        from services.wheelhouse.shared.paint_overlay import PaintOverlayEvent

        snapshot_id = effect.snapshot_id
        summary = self._overlay_keepalive_summary(snapshot_id)
        if summary is None:
            logger.warning(
                "overlay: paint requested for snapshot %r but no summary is "
                "cached; skipping paint (gen=(%s,%s), trace_id=%s)",
                snapshot_id, effect.overlay_session_id,
                effect.paint_generation, trace_id,
            )
            return
        event = PaintOverlayEvent(
            overlay_session_id=effect.overlay_session_id,
            paint_generation=effect.paint_generation,
            summary=summary,
        )
        self._put_overlay_gui_action(event.to_dict(), trace_id=trace_id)

    async def _overlay_dispatch_clear_one(self, effect, trace_id: str) -> None:
        """Put a clear_overlay action on the GUI queue (wh-n29v.95)."""
        from services.wheelhouse.shared.clear_overlay import ClearOverlayEvent

        event = ClearOverlayEvent(
            overlay_session_id=effect.overlay_session_id,
            paint_generation=effect.paint_generation,
        )
        self._put_overlay_gui_action(event.to_dict(), trace_id=trace_id)

    def _put_overlay_gui_action(self, gui_msg: dict, *, trace_id: str) -> None:
        """Put a paint/clear action on the GUI state queue, degrade on failure."""
        if not self.state_manager or not hasattr(
            self.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "overlay: %s dropped, no state_manager queue (trace_id=%s)",
                gui_msg.get("action"), trace_id,
            )
            return
        try:
            self.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "overlay: %s dropped, GUI queue put failed (trace_id=%s): %s",
                gui_msg.get("action"), trace_id, exc,
            )

    def _put_overlay_walk_cue(
        self, active: bool, *, trace_id: str, walk_timeout_ms=None,
    ) -> None:
        """Put an overlay_walk_cue action on the GUI state queue (wh-n29v.117).

        A plain-dict side-channel notification (no shared/ schema) that drives
        the floating-button "walking" progress cue while a numbered-overlay
        walk is in flight. Reuses the same defensive enqueue path as the
        paint/clear effects so it rides the same channel and ordering, and
        degrades quietly (the cue is cosmetic) if the queue is unavailable.

        wh-n29v.118: on the active:True emit, ``walk_timeout_ms`` carries the
        effective Logic walk bound (response_timeout_ms) so the GUI fallback
        timer outlasts the real walk. The active:False emit omits it (the GUI
        ignores it on the clear path).
        """
        msg: dict = {
            "action": "overlay_walk_cue",
            "active": bool(active),
            "trace_id": trace_id,
        }
        if walk_timeout_ms is not None:
            msg["walk_timeout_ms"] = walk_timeout_ms
        self._put_overlay_gui_action(msg, trace_id=trace_id)

    def _overlay_keepalive_summary(self, snapshot_id):
        """Resolve ``snapshot_id`` and re-put it to keep it alive past TTL (wh-n29v.95).

        PART 4 / criterion 4: a snapshot the overlay keeps visible must not age
        out of the resolver cache. The machine pins/paints a snapshot only while
        it is meant to be on screen, so re-putting on every pin/paint access
        resets the TTL window for the lifetime of the visible overlay. A
        SNAPSHOT_EXPIRED on a still-visible overlay would otherwise misreport
        'no badge N'. Returns the summary, or None when the snapshot is genuinely
        gone (never stored, or evicted under max_entries pressure).
        """
        if not snapshot_id:
            return None
        result = self.click_snapshot_summary_cache.resolve(snapshot_id)
        from services.wheelhouse.click_snapshot_summary_cache import CacheStatus

        if result.status is not CacheStatus.HIT or result.summary is None:
            return None
        # Re-put resets the TTL window (keepalive): the entry is still visible.
        try:
            self.click_snapshot_summary_cache.put(snapshot_id, result.summary)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "overlay: snapshot keepalive re-put failed for %r: %s",
                snapshot_id, exc,
            )
        return result.summary

    async def _overlay_send_pin(self, effect, trace_id: str) -> None:
        """Send pin_snapshot to Input and assign the tracked identity (wh-n29v.95).

        PART 5 / criterion 5: the pin point is when the overlay becomes
        visible/paused, so capture and store ``_overlay_tracked_identity`` here
        so the destroy-while-paused hook can register and the resume identity
        check works. The pin keepalive also re-puts the summary so the
        actively-pinned snapshot survives past TTL (criterion 4). Paint
        CORRECTNESS does not depend on this ack; it is consumed for bookkeeping
        only. Note (wh-n29v.120.1), however, that the ack await DOES delay paint
        dispatch in wall-clock time: PIN_SNAPSHOT runs before DISPATCH_PAINT in
        the same FIFO-locked effect batch, so a slow/hung pin ack postpones
        paint_overlay by up to response_timeout_ms. The walking-cue fallback
        timer (wh-n29v.117) is sized to account for this (it carries
        2 * response_timeout_ms as walk_timeout_ms).
        """
        # Capture the foreground identity the overlay is being built for.
        try:
            self._overlay_tracked_identity = (
                self._capture_overlay_foreground_identity()
            )
            # Remember it per snapshot too (trigger B): the prior, still-visible
            # snapshot during a focus-change refresh needs its OWN recorded
            # window, not the latest pin's. Skip a None identity (undeterminable
            # -> routing falls back to its resolve-against-visible default).
            if effect.snapshot_id and self._overlay_tracked_identity is not None:
                self._overlay_snapshot_window_identity[effect.snapshot_id] = (
                    self._overlay_tracked_identity
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "overlay: failed to capture tracked identity at pin "
                "(trace_id=%s): %s", trace_id, exc,
            )
        # Keepalive the pinned (visible) snapshot.
        self._overlay_keepalive_summary(effect.snapshot_id)
        await self._overlay_send_pin_request(
            "pin_snapshot",
            {
                "overlay_session_id": effect.overlay_session_id,
                "snapshot_id": effect.snapshot_id or "",
                "paint_generation": effect.paint_generation,
            },
            trace_id=trace_id,
        )

    async def _overlay_send_unpin(self, effect, trace_id: str) -> None:
        """Send unpin_snapshot to Input (clear-by-identity) (wh-n29v.95)."""
        # Drop the per-snapshot window identity for the unpinned snapshot
        # (trigger B bookkeeping); the deferred prior unpin fires when a refresh
        # completes, so the prior's entry stays until then -- exactly while it is
        # the visible snapshot.
        if effect.snapshot_id:
            self._overlay_snapshot_window_identity.pop(effect.snapshot_id, None)
        await self._overlay_send_pin_request(
            "unpin_snapshot",
            {
                "overlay_session_id": effect.overlay_session_id,
                "snapshot_id": effect.snapshot_id or "",
            },
            trace_id=trace_id,
        )

    async def _overlay_send_refresh(
        self, snapshot_id: str, overlay_session_id: int, trace_id: str,
    ) -> None:
        """Send refresh_overlay_snapshot to Input to slide the store TTL.

        The Input-store counterpart to ``_overlay_keepalive_summary`` (which
        slides only the Logic resolver cache). The numbered overlay's snapshot
        lives in TWO independent 30s-TTL stores -- the Logic resolver cache and
        the Input-process ElementFinder store, both keyed off
        ``[click] snapshot_ttl_seconds``. Before this, the 15s keepalive re-put
        only the Logic cache; the Input copy aged out while Logic kept resolving
        and dispatching "click N", so the click failed with snapshot_expired on a
        still-visible overlay (wh-overlay-snapshot-keepalive trigger A). This
        sends the Input side a refresh so both stores slide together.

        Best-effort, like pin/unpin: a lost ack does not corrupt state (the
        store ages out normally from its last refresh if this never lands).
        """
        if not snapshot_id:
            return
        await self._overlay_send_pin_request(
            "refresh_overlay_snapshot",
            {
                "overlay_session_id": overlay_session_id,
                "snapshot_id": snapshot_id,
            },
            trace_id=trace_id,
        )

    async def _overlay_send_pin_request(
        self, action: str, params: dict, *, trace_id: str,
    ) -> None:
        """Send a pin/unpin request and consume the PinSnapshotResponse ack.

        Best-effort bookkeeping: Logic does not block the paint on this ack. A
        timeout / send failure / malformed reply logs and is dropped (the pin is
        defence-in-depth over LRU + TTL, so a lost ack does not corrupt state).
        """
        from utils.trace_context import set_trace

        set_trace(trace_id)
        timeout_ms = self.click_config.response_timeout_ms
        timeout_s = max(timeout_ms / 1000.0, 0.1)
        try:
            raw = await self.app.send_request(
                action, params=params, timeout_s=timeout_s,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "overlay: %s no reply within %dms (trace_id=%s)",
                action, timeout_ms, trace_id,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- CancelledError propagates
            logger.warning(
                "overlay: %s send_request failed (trace_id=%s): %s",
                action, trace_id, exc,
            )
            return
        from services.wheelhouse.shared.pin_snapshot import (
            PinSnapshotResponse,
            PinSnapshotResponseSchemaError,
        )

        try:
            resp = PinSnapshotResponse.from_dict(raw)
        except (
            PinSnapshotResponseSchemaError, ValueError, KeyError, TypeError,
        ) as exc:
            logger.warning(
                "overlay: malformed %s response (trace_id=%s): %s",
                action, trace_id, exc,
            )
            return
        if resp.status != "ok" or (action == "pin_snapshot" and not resp.pinned):
            logger.debug(
                "overlay: %s ack status=%s reason=%s pinned=%s "
                "(snapshot=%s, trace_id=%s)",
                action, resp.status, resp.reason, resp.pinned,
                resp.snapshot_id, trace_id,
            )

    def _overlay_fire_notice(self, effect, trace_id: str) -> None:
        """Fire a FIRE_NOTICE effect (wh-n29v.95).

        A FIRE_NOTICE carrying a ClickNoticeEvent forwards that suppressed
        notice (the auto-open ambiguous-click notice). A FIRE_NOTICE whose
        ``notice`` is None is the marker for the generic standalone
        "numbers couldn't be drawn" notice for a failed standalone walk
        (wh-n29v.16.1): build an execution_failed ClickNoticeEvent here.
        """
        notice = effect.notice
        if notice is not None:
            self._forward_click_notice(
                outcome=notice.outcome,
                reason=notice.reason,
                matched_name=notice.matched_name,
                matched_names=notice.matched_names,
                spoken_name=notice.spoken_name,
                snapshot_id=notice.snapshot_id,
                trace_id=notice.trace_id or trace_id,
            )
            return
        # Generic standalone walk-failure notice.
        self._forward_click_notice(
            outcome="execution_failed",
            reason="numbers_not_drawn",
            matched_name=None,
            matched_names=(),
            spoken_name="",
            snapshot_id=None,
            trace_id=trace_id,
        )

    def _overlay_arm_timer(self, effect, trace_id: str) -> None:
        """Arm the single per-state timeout timer (wh-n29v.95).

        Cancels any existing timer first (exactly one timer is live at a time;
        the machine emits CANCEL_TIMER before a new ARM where needed, but cancel
        here too as a belt-and-suspenders). Schedules a ``loop.call_later`` for
        ``effect.duration_ms`` that, on fire, feeds a TIMEOUT OverlayEvent at the
        ARMED (overlay_session_id, paint_generation) through
        ``_apply_overlay_event``. The machine's pre-table generation gate drops
        the TIMEOUT if the generation has since advanced, so a stale armed timer
        cannot abort a newer walk.
        """
        existing = getattr(self, "_overlay_timer", None)
        if existing is not None:
            existing.cancel()
        sid = effect.overlay_session_id
        gen = effect.paint_generation
        self._overlay_timer_pair = (sid, gen)
        self._overlay_armed_timer_state = effect.timer_state
        delay_s = max(effect.duration_ms / 1000.0, 0.0)
        self._overlay_timer = self.loop.call_later(
            delay_s, lambda: self._fire_overlay_timeout(sid, gen, trace_id),
        )

    def _overlay_cancel_timer(self) -> None:
        """Cancel the current per-state timeout timer, if any (wh-n29v.95)."""
        existing = getattr(self, "_overlay_timer", None)
        if existing is not None:
            existing.cancel()
        self._overlay_timer = None
        self._overlay_timer_pair = None
        self._overlay_armed_timer_state = None

    def _fire_overlay_timeout(self, sid: int, gen: int, trace_id: str) -> None:
        """Timer callback: feed a TIMEOUT OverlayEvent at the armed pair (wh-n29v.95)."""
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        self._overlay_timer = None
        self._overlay_timer_pair = None
        self._overlay_armed_timer_state = None
        self._apply_overlay_event(
            OverlayEvent(
                kind=OverlayEventKind.TIMEOUT,
                overlay_session_id=sid,
                paint_generation=gen,
            ),
            source=f"timeout gen=({sid},{gen})",
        )

    # ------------------------------------------------------------------
    # wh-n29v.21: Logic-side focus-hook wiring (foreground + transient destroy
    # + resume full-identity check). Started in main(), stopped in shutdown().
    # ------------------------------------------------------------------
    def _start_overlay_focus_hooks(self) -> None:
        """Start the overlay focus-hook thread when the overlay is active.

        Mirrors window_mover's start: a background daemon thread owns the
        message-only window, the FOREGROUND ``SetWinEventHook``, and the
        transient destroy hook. The callbacks marshal onto this Logic loop via
        ``call_soon_threadsafe``. Best-effort: a failure to start logs and
        leaves overlay focus-following disabled rather than aborting startup.
        Gated on BOTH ``click_config.enabled`` AND
        ``click_config.overlay_enabled_effective``: when voice clicking is off
        (master opt-out or a bad Phase 1 key) OR the overlay alone is off (a bad
        overlay key, or an ``overlay_enabled=false`` opt-out) the hook is not
        started (no IPC, no walk, no focus following). A valid ``enabled=false``
        leaves ``overlay_enabled_effective`` True, so the ``enabled`` term is
        required. This matches the Input-side overlay walk gate
        (``ui_action_handler._get_overlay_walk_finder``) so the Logic and Input
        processes agree on whether the overlay is on; by-name click stays gated
        on ``enabled`` alone.
        """
        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            logger.info(
                "overlay: focus hooks not started (numbered overlay not "
                "active; enabled=%s, overlay_invalid_key=%s).",
                self.click_config.enabled,
                self.click_config.overlay_invalid_key,
            )
            return
        if self._overlay_focus_hooks is not None:
            return
        try:
            manager = OverlayFocusHookManager(
                loop=self.loop,
                on_foreground=self._on_overlay_foreground_change,
                on_destroy=self._on_overlay_focused_hwnd_destroyed,
                on_menu_popup=self._on_overlay_menu_popup_change,
            )
            if manager.start():
                self._overlay_focus_hooks = manager
            else:
                logger.warning(
                    "overlay: focus-hook manager failed to start; overlay "
                    "focus following is disabled this session.",
                )
        except Exception as exc:  # noqa: BLE001 -- never abort startup
            logger.error(
                "overlay: error starting focus hooks: %s", exc, exc_info=True,
            )

    def _stop_overlay_focus_hooks(self) -> None:
        """Stop the focus-hook thread and unhook all hooks (no leaked hooks)."""
        manager = self._overlay_focus_hooks
        if manager is None:
            return
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001 -- never block shutdown
            logger.debug("overlay: error stopping focus hooks: %s", exc)
        self._overlay_focus_hooks = None
        self._overlay_destroy_hook_active = False
        self._cancel_overlay_settle_refire()

    def _build_overlay_focus_debouncer(self) -> "FocusChangeDebouncer":
        """Build the focus-change debouncer from the VALIDATED overlay config.

        The debounce interval is ``self.click_config.overlay_focus_debounce_ms``
        -- the value the ``ClickConfig`` validator already range-checked against
        ``[0, 5000]`` (an out-of-range raw value degrades to the 250 default and
        disables the overlay). Reading it through the validated config rather
        than re-reading the raw ``[click]`` block guarantees the Logic process
        and the Input process derive the SAME debounce from the same raw block,
        so they can never disagree on the validated config (wh-n29v.66). This is
        a small method so the wiring is testable via the
        ``MagicMock(spec=LogicController)`` bound-method pattern without driving
        the heavy full ``__init__``.
        """
        from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

        return FocusChangeDebouncer(
            debounce_ms=self.click_config.overlay_focus_debounce_ms
        )

    def _on_overlay_foreground_change(self, hwnd: int) -> None:
        """Loop-marshalled FOREGROUND callback: debounce, then feed FOCUS_CHANGE.

        Runs on the Logic asyncio loop (the hook thread already marshalled
        here). Applies the pure debounce; a coalesced event returns without
        touching the machine. On a real change it maps the raw event to a
        ``FOCUS_CHANGE`` ``OverlayEvent`` and applies it via
        ``_apply_overlay_event`` (which gates its trace-id mint and INFO log on
        the outcome -- a closed-state NO_OP, the all-day common case, logs only
        DEBUG). The debounce only COALESCES rapid foreground callbacks
        (tooltip-as-foreground artifacts, Alt-Tab steps); it does NOT enforce
        generation supersession -- that is a wire-level guarantee of the
        ``(overlay_session_id, paint_generation)`` gate inside the state machine
        (design v4 "Supersession generation"), not a producer-side check here.
        """
        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            return
        import time

        from services.wheelhouse.overlay_focus_hooks import map_foreground_event

        now_ms = time.monotonic() * 1000.0
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            logger.debug(
                "overlay: foreground change to hwnd=%s coalesced by debounce.",
                hwnd,
            )
            self._arm_overlay_settle_refire("foreground change")
            return
        self._cancel_overlay_settle_refire()
        self._apply_overlay_event(
            map_foreground_event(hwnd=hwnd),
            source=f"foreground change hwnd={hwnd}",
        )

    def _on_overlay_menu_popup_change(self, event_id: int) -> None:
        """Loop-marshalled MENUPOPUP callback: map, debounce, feed FOCUS_CHANGE.

        wh-overlay-menu-close-stale: a menu closing (or opening) over the
        focused window changes what is clickable WITHOUT a foreground change,
        so the overlay's badge set must be rebuilt exactly as a focus change
        rebuilds it -- ``map_menu_popup_event`` documents why reusing
        ``FOCUS_CHANGE`` is the deliberate design. Mapping runs BEFORE the
        debounce so an id the mapper rejects cannot burn the debounce window
        (a real menu-close right after would be coalesced and the stale
        badges would survive). The debouncer is SHARED with the foreground
        callback on purpose: both event sources mean "re-walk against current
        reality", so one walk serves a focus change and a menu event landing
        together. Unlike the foreground callback this does NOT touch the
        tracked identity -- the foreground window is unchanged.
        """
        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            return
        from services.wheelhouse.overlay_focus_hooks import (
            EVENT_SYSTEM_MENUPOPUPEND,
            map_menu_popup_event,
        )

        event = map_menu_popup_event(event_id=event_id)
        if event is None:
            return
        import time

        now_ms = time.monotonic() * 1000.0
        verb = "closed" if event_id == EVENT_SYSTEM_MENUPOPUPEND else "opened"
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            logger.debug(
                "overlay: menu pop-up event %#06x coalesced by debounce.",
                event_id,
            )
            self._arm_overlay_settle_refire(f"menu popup {verb}")
            return
        self._cancel_overlay_settle_refire()
        self._apply_overlay_event(event, source=f"menu popup {verb}")

    def _arm_overlay_settle_refire(self, source: str) -> None:
        """Arm ONE trailing settle timer for the debounce-window remainder.

        wh-overlay-nested-dupes.1.1: the drop-only debounce loses the FINAL
        event of a burst -- double-Escape out of a submenu (the second close is
        coalesced, dead parent-menu badges persist), open-then-instant-dismiss,
        and a menu action whose new dialog's foreground event lands inside the
        window the menu close just anchored. While painted, the debounced
        events are the ONLY thing that rebuilds the badge set, so a lost final
        event leaves stale badges indefinitely. The settle timer fires at the
        window edge and re-applies one FOCUS_CHANGE, so a burst still costs at
        most one extra walk while the final state is never lost. At most one
        settle is pending; a real (non-coalesced) fire cancels it.

        Runs only on the Logic asyncio loop (the hook callbacks marshal here,
        and ``loop.call_later`` callbacks run here), so no lock is needed.

        Never arms while the machine is CLOSED (wh-overlay-nested-dupes.1.5):
        with no overlay session there are no badges to clean up, so a settle
        serves no purpose -- and a timer armed during the CLOSED period could
        outlive it, surviving into a 'show numbers' / auto-open session that
        starts inside the remaining window and superseding that session's
        user-requested walk with a stale FOCUS_CHANGE. Together with the
        cancel-on-CLOSED reconciler this enforces the invariant "no settle is
        ever pending while CLOSED", so no stale settle can cross a session
        boundary. The gate also covers the timer body's re-arm path.
        """
        if self._overlay_settle_handle is not None:
            return
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or machine.state is OverlayState.CLOSED:
            return
        import time

        delay_ms = self._overlay_focus_debouncer.remaining_ms(
            now_ms=time.monotonic() * 1000.0
        )
        self._overlay_settle_handle = self.loop.call_later(
            delay_ms / 1000.0, self._on_overlay_settle_refire, source,
        )

    def _on_overlay_settle_refire(self, source: str) -> None:
        """The settle timer body: apply the deferred FOCUS_CHANGE, or re-arm.

        If the debounce window is still hot (a real event fired after this
        timer was armed and advanced the anchor), do not touch the machine --
        re-arm for the new remainder so the guarantee ("the burst's final
        state gets one walk") still holds. Otherwise fire one FOCUS_CHANGE
        through the normal path; a closed/paused/error machine treats it as
        the usual record-only no-op.
        """
        self._overlay_settle_handle = None
        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            return
        import time

        now_ms = time.monotonic() * 1000.0
        if not self._overlay_focus_debouncer.should_fire(now_ms=now_ms):
            self._arm_overlay_settle_refire(source)
            return
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
        )

        self._apply_overlay_event(
            OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE),
            source=f"{source} (settled)",
        )

    def _cancel_overlay_settle_refire(self) -> None:
        """Cancel a pending settle timer (a real fire made it redundant)."""
        handle = self._overlay_settle_handle
        if handle is not None:
            handle.cancel()
            self._overlay_settle_handle = None

    def _on_overlay_focused_hwnd_destroyed(self, destroyed_hwnd: int) -> None:
        """Loop-marshalled DESTROY callback: feed FOCUSED_HWND_DESTROYED if tracked.

        Runs on the Logic loop. The transient destroy hook is scoped to the
        tracked window's pid/tid, but a process emits ``EVENT_OBJECT_DESTROY``
        for many objects; ``map_destroy_event`` narrows to the EXACT tracked
        top-level HWND. A non-matching or untracked destroy is dropped. A match
        feeds ``FOCUSED_HWND_DESTROYED`` (drives ``paused -> closed``).
        """
        if not (
            self.click_config.enabled
            and self.click_config.overlay_enabled_effective
        ):
            return
        # wh-n29v.24.2: drop a destroy callback that arrives after the transient
        # hook was logically unregistered. The hook is unregistered
        # asynchronously (``unregister_destroy_hook`` POSTs to the hook thread,
        # which processes it on its ~20 ms PeekMessage poll), so a destroy
        # firing in the gap between leaving ``paused`` and the worker thread
        # processing the unregister would otherwise feed FOCUSED_HWND_DESTROYED
        # into a non-``paused`` machine -> INVALID_TRANSITION -> ERROR. The hook
        # is live only while ``paused``; ``_reconcile_overlay_destroy_hook``
        # clears ``_overlay_destroy_hook_active`` synchronously on this same loop
        # when leaving ``paused``, so a False flag here means we have already
        # requested the unregister and any destroy callback is a stale artifact
        # of that gap. Dropping it keeps the state machine's fail-closed contract
        # intact and does not block the loop.
        if not self._overlay_destroy_hook_active:
            return
        from services.wheelhouse.overlay_focus_hooks import map_destroy_event

        tracked = self._overlay_tracked_identity
        tracked_hwnd = tracked.hwnd if tracked is not None else 0
        event = map_destroy_event(
            destroyed_hwnd=destroyed_hwnd, tracked_hwnd=tracked_hwnd,
        )
        if event is None:
            return
        self._apply_overlay_event(
            event,
            source=(
                f"tracked window hwnd={destroyed_hwnd} destroyed while paused"
            ),
        )

    def _apply_overlay_event(self, event, *, source: str) -> None:
        """Apply an OverlayEvent, hand off effects, reconcile session-end state.

        Single Logic-loop entry point for the focus-hook-produced events
        (FOCUS_CHANGE, FOCUSED_HWND_DESTROYED). The machine is pure; this
        applies the event, hands any returned effects to the
        ``_perform_overlay_effects`` seam (the integration bead performs the
        real IPC), resets the focus debouncer when the session ends (entry to
        ``closed``), and reconciles the transient destroy hook against the
        RESULTING machine state -- registering it on entry to ``paused`` and
        unregistering it on leaving ``paused`` -- so the hook is live exactly
        while the overlay is paused (bounded cost, no leaked hooks).

        Logging is outcome-gated (wh-n29v.22.3): the overwhelmingly common case
        is a foreground change while the machine is ``closed`` (a NO_OP). That
        case logs a single DEBUG line and mints NO trace id, so ordinary all-day
        window switching neither spews INFO lines nor churns uuids. Only an
        event that actually changes state or carries effects mints a trace id
        and logs at INFO.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayOutcome,
            OverlayState,
        )

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        prev_state = machine.state
        result = machine.apply(event)
        if result.outcome is OverlayOutcome.NO_OP and not result.effects:
            logger.debug(
                "overlay: %s -> %s no_op (state=%s)",
                source, event.kind.value, machine.state.value,
            )
        else:
            trace_id = self._mint_overlay_trace_id()
            logger.info(
                "overlay: %s -> applied %s state=%s outcome=%s "
                "(%d effect(s), trace_id=%s)",
                source, event.kind.value, machine.state.value,
                result.outcome.value, len(result.effects), trace_id,
            )
            if result.effects:
                self._perform_overlay_effects(result.effects, trace_id=trace_id)
        # wh-n29v.22.2 / wh-n29v.23.2: clear the debounce anchor only on the
        # actual TRANSITION into closed (a session ending), not whenever the
        # machine merely happens to already be closed. Resetting on every
        # closed-state foreground change would defeat the debounce for the
        # all-day idle path (each event would fire as a first event).
        if (
            machine.state is OverlayState.CLOSED
            and prev_state is not OverlayState.CLOSED
        ):
            self._overlay_focus_debouncer.reset()
        # wh-n29v.121: stamp the LATEST transition into PAINTED so the browser
        # proactive refresh measures age from the most recent paint. An edge,
        # not a level: re-stamping on every apply while already PAINTED would
        # never let the age reach the trust window.
        if (
            machine.state is OverlayState.PAINTED
            and prev_state is not OverlayState.PAINTED
        ):
            self._overlay_last_paint_monotonic = self._overlay_now_monotonic()
        # wh-overlay-fixqueue-review.1/.2: attribute each refresh to its
        # trigger on the way IN, process the outcome on the way OUT.
        if (
            machine.state is OverlayState.REFRESH_IN_FLIGHT
            and prev_state is not OverlayState.REFRESH_IN_FLIGHT
        ):
            # The pin is still the prior snapshot here: the machine's
            # _refresh only bumps the generation; the pin changes later at
            # BUILD_RESPONSE. A supersede while already in flight keeps the
            # original attribution (no edge fires).
            self._overlay_refresh_started_proactive = bool(
                getattr(self, "_overlay_in_proactive_apply", False)
            )
            self._overlay_refresh_entry_pin = machine.pinned_snapshot_id
        if (
            prev_state is OverlayState.REFRESH_IN_FLIGHT
            and machine.state is not OverlayState.REFRESH_IN_FLIGHT
        ):
            if getattr(self, "_overlay_refresh_started_proactive", False):
                if machine.state is OverlayState.PAINTED:
                    entry_pin = getattr(
                        self, "_overlay_refresh_entry_pin", None,
                    )
                    backoff = max(
                        int(getattr(
                            self, "_overlay_browser_refresh_backoff", 1,
                        )),
                        1,
                    )
                    if machine.pinned_snapshot_id != entry_pin:
                        # Genuine swap: fresh badges are on screen. Reset the
                        # back-off and arm the renumber guard so a "click N"
                        # spoken against the PRE-swap badges is checked
                        # before it resolves against the renumbered overlay.
                        self._overlay_browser_refresh_backoff = 1
                        self._overlay_proactive_swap = (
                            entry_pin, self._overlay_now_monotonic(),
                        )
                    else:
                        # Restore (failed build/paint/timeout): the badges
                        # are unchanged, so no guard -- but back off the next
                        # proactive attempt so a consistently failing walk
                        # is not re-run every window forever.
                        self._overlay_browser_refresh_backoff = min(
                            backoff * 2, 8,
                        )
                        self._overlay_proactive_swap = None
                # Exits to CLOSED/PAUSED/ERROR change no back-off state;
                # the CLOSED edge above resets it for the next session.
            self._overlay_refresh_started_proactive = False
            self._overlay_refresh_entry_pin = None
        self._reconcile_overlay_destroy_hook(
            machine.state is OverlayState.PAUSED
        )
        # wh-n29v.95 part 5: clear the tracked identity on entry to closed so
        # the destroy-while-paused hook and the resume identity check do not see
        # a stale identity from a torn-down session.
        self._reconcile_overlay_tracked_identity()
        # wh-n29v.96.2: arm/cancel the periodic visible-snapshot keepalive to
        # match the resulting PAINTED/PAUSED-ness (a focus-change refresh, a
        # paint-ack, a mic-pause, or a timeout can all change visibility here).
        self._reconcile_overlay_keepalive_timer()
        # wh-overlay-fixqueue-review.1/.2: reset the proactive-refresh back-off
        # and renumber guard when this event closed the session.
        self._reconcile_overlay_browser_refresh_reset()
        # wh-n29v.101.1: consume or clear a deferred post-click refresh now that
        # the machine state has settled (a paint-ack may have reached PAINTED, a
        # supersede may have bumped the generation, or this event may have closed
        # the session). Runs last so it observes the final post-apply state.
        self._reconcile_overlay_pending_postclick_refresh()
        # wh-n29v.114.1: clear a stale auto-open item_id_filter stash on entry to
        # closed (shared with handle_overlay_command, which bypasses this method).
        self._reconcile_overlay_auto_open_filter()
        # wh-overlay-nested-dupes.1.4: a pending settle re-fire belongs to the
        # session that armed it; cancel it once the machine is closed (shared
        # with handle_overlay_command, which bypasses this method).
        self._reconcile_overlay_settle_refire()

    def _reconcile_overlay_settle_refire(self) -> None:
        """Cancel a pending settle re-fire when the machine is closed (wh-overlay-nested-dupes.1.4).

        A pending settle timer belongs to the overlay session that armed it.
        Both Logic-side paths that can drive the machine to CLOSED must cancel
        it: ``_apply_overlay_event`` (focus-hook events, paint-acks) and
        ``handle_overlay_command`` (the show/hide voice command, which applies
        HIDE_NUMBERS directly and bypasses ``_apply_overlay_event``). If the
        timer survived a hide, an immediate 'show numbers' would let the stale
        timer fire a FOCUS_CHANGE into the fresh session's in-flight build and
        restart it -- an unnecessary second walk and generation bump belonging
        to the previous session. Mirrors the sibling ``_reconcile_overlay_*``
        helpers: reads the machine's CURRENT state (not a prev/next edge), so
        it is idempotent and safe to call after every apply. Arming never
        happens inside an apply (only the hook callbacks and the timer body
        arm), so this can never cancel a timer armed by the same apply it
        follows.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is not None and machine.state is OverlayState.CLOSED:
            self._cancel_overlay_settle_refire()

    def _reconcile_overlay_auto_open_filter(self) -> None:
        """Clear the auto-open item_id_filter stash when the machine is closed (wh-n29v.114.1).

        The stash (``_overlay_auto_open_filter``) is set only while an auto-open
        is in flight (the machine left CLOSED via AUTO_OPEN, then
        ``_perform_auto_open_ambiguous`` recorded the finalist filter). When the
        machine is CLOSED there is no in-flight auto-open, so any remaining stash
        is stale and must be dropped before it can leak into a later session.
        Both Logic-side closed-entry paths call this: ``_apply_overlay_event``
        (focus-hook events, paint-acks) and ``handle_overlay_command`` (the
        show/hide voice command, which applies HIDE_NUMBERS directly and bypasses
        ``_apply_overlay_event``). Mirrors the other ``_reconcile_overlay_*``
        helpers -- reading the CURRENT state (not a prev/next edge) is idempotent
        and safe to call after every apply.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is not None and machine.state is OverlayState.CLOSED:
            self._overlay_auto_open_filter = None

    def _reconcile_overlay_tracked_identity(self) -> None:
        """Clear ``_overlay_tracked_identity`` when the machine is closed (wh-n29v.95).

        PART 5 / criterion 5: the tracked identity is assigned at the pin point
        (``_overlay_send_pin``) when the overlay becomes visible/paused and must
        be cleared to ``None`` on entry to closed, so the transient destroy hook
        does not register against a torn-down window and the resume identity
        check fails closed to re-walk. Reading the machine's CURRENT state (not a
        prev/next edge) is idempotent and safe to call after every apply.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state is OverlayState.CLOSED:
            self._overlay_tracked_identity = None
            # No snapshot is visible once closed; drop the whole per-snapshot
            # window-identity map so it cannot leak across overlay sessions
            # (trigger B bookkeeping).
            self._overlay_snapshot_window_identity.clear()

    def _reconcile_overlay_keepalive_timer(self) -> None:
        """Arm/cancel the periodic visible-snapshot keepalive (wh-n29v.96.2).

        FINDING 2 / criterion 4: PAINTED and PAUSED are steady NO_TIMEOUT states
        with no recurring pin/paint, so the one-shot pin/paint keepalive cannot
        keep a quiescent overlay's summary alive. Arm a periodic timer whenever
        the machine is in PAINTED/PAUSED with a pinned snapshot, and cancel it
        otherwise. Reads the machine's CURRENT state (not an edge), so it is
        idempotent and safe to call after every apply. The timer re-puts the
        pinned summary and reschedules itself, so a visible overlay idle past the
        snapshot TTL stays resolvable.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        # wh-n29v.98.3: truthy guard, matching the sibling early return in
        # _overlay_keepalive_summary (`if not snapshot_id: return None`). An
        # empty-string pin is a schema-prevented invariant violation, but this
        # integration-layer guard should not rely on the build path's
        # validation alone -- a falsy pin must not arm a keepalive timer that
        # would fire every interval as a no-op.
        wants = (
            machine.state in (OverlayState.PAINTED, OverlayState.PAUSED)
            and bool(machine.pinned_snapshot_id)
        )
        if wants:
            if getattr(self, "_overlay_keepalive_timer", None) is None:
                # wh-overlay-snapshot-keepalive (residual edge): the FIRST
                # tick fires immediately, not a full interval out. When a
                # FAILED refresh restores the prior snapshot, that snapshot's
                # ttl_anchor was last slid up to ~one interval before the
                # refresh began; a fresh full interval here could put the
                # next slide past the TTL, where refresh_snapshot_ttl fails
                # closed and a click on the still-visible restored overlay
                # misses. The immediate tick slides the restored snapshot
                # right away (a harmless extra re-put on a first paint) and
                # its body re-arms the periodic interval as usual.
                self._overlay_keepalive_timer = self.loop.call_soon(
                    self._fire_overlay_keepalive,
                )
        else:
            self._overlay_cancel_keepalive_timer()

    def _arm_overlay_keepalive_timer(self) -> None:
        """Schedule the next periodic keepalive re-put (wh-n29v.96.2)."""
        interval = getattr(self, "_overlay_keepalive_interval_s", 15.0)
        self._overlay_keepalive_timer = self.loop.call_later(
            interval, self._fire_overlay_keepalive,
        )

    def _overlay_cancel_keepalive_timer(self) -> None:
        """Cancel the periodic keepalive timer, if any (wh-n29v.96.2)."""
        existing = getattr(self, "_overlay_keepalive_timer", None)
        if existing is not None:
            existing.cancel()
        self._overlay_keepalive_timer = None

    def _fire_overlay_keepalive(self) -> None:
        """Keepalive-timer callback: re-put the pinned summary and reschedule.

        Re-puts the machine's currently-pinned (visible) snapshot summary to
        slide its TTL window, then reschedules itself while the machine is still
        in PAINTED/PAUSED. Best-effort: any error logs and does NOT escalate
        (this runs directly on the loop, not under a task done-callback). The
        timer is reschuduled ONLY while the overlay is still visible, so it
        cannot leak past teardown.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        self._overlay_keepalive_timer = None
        machine = getattr(self, "click_overlay_state", None)
        if machine is None:
            return
        if machine.state not in (OverlayState.PAINTED, OverlayState.PAUSED):
            return
        try:
            self._overlay_keepalive_summary(machine.pinned_snapshot_id)
        except Exception as exc:  # noqa: BLE001 -- never crash the loop
            logger.debug("overlay: periodic keepalive re-put failed: %s", exc)
        # Slide the Input-process store TTL too, not just the Logic cache: both
        # expire independently 30s after the walk, and "click N" dispatches
        # against the Input store. Fire-and-forget (Logic does not block on the
        # ack); the send swallows its own errors. wh-overlay-snapshot-keepalive.
        pinned = machine.pinned_snapshot_id
        if pinned:
            try:
                self.create_task_with_error_handling(
                    self._overlay_send_refresh(
                        pinned,
                        machine.overlay_session_id,
                        self._mint_overlay_trace_id(),
                    ),
                    "overlay_keepalive_refresh",
                )
            except Exception as exc:  # noqa: BLE001 -- never crash the loop
                logger.debug(
                    "overlay: periodic keepalive store-refresh schedule "
                    "failed: %s", exc,
                )
        # Reschedule for the next interval while still visible.
        self._arm_overlay_keepalive_timer()
        # wh-n29v.121: LAST, so the re-arm above is already in place -- if this
        # feeds a refresh, the apply's keepalive reconciler cancels the timer
        # (state leaves PAINTED) and re-arms it when the refresh completes.
        self._maybe_overlay_browser_refresh(machine)

    def _overlay_now_monotonic(self) -> float:
        """Monotonic-clock seam for the overlay timers (fake clock in tests)."""
        import time

        return time.monotonic()

    def _maybe_overlay_browser_refresh(self, machine) -> None:
        """Feed one refresh when a painted overlay over a browser goes stale.

        wh-n29v.121: dynamic Chromium/Brave pages shift layout while the user
        idles, so the first "click N" after an in-page shift is refused once
        before the reactive re-walk repairs the badges. Called from each
        keepalive tick; when the machine is PAINTED (deliberately NOT PAUSED --
        a paused overlay is invisible), the tracked window's process is in the
        effective browser-process set, and the latest PAINTED entry is older
        than ``overlay_browser_refresh_seconds``, feed one FOCUS_CHANGE -- the
        same event the focus/menu hooks reuse, which maps to a REFRESH in
        PAINTED -- so generation discipline, pin handoff, and repaint all run
        through the machine normally. A "click N" racing the refresh is HELD
        by the in-flight routing rule, and stale acks are already rejected.
        """
        from services.wheelhouse.click_overlay_state import (
            OverlayEvent,
            OverlayEventKind,
            OverlayState,
        )

        window_s = getattr(self, "_overlay_browser_refresh_seconds", 0.0)
        if window_s <= 0:
            return
        if machine.state is not OverlayState.PAINTED:
            return
        tracked = getattr(self, "_overlay_tracked_identity", None)
        if tracked is None or not tracked.process_name:
            return
        browsers = getattr(self, "_overlay_browser_process_set", frozenset())
        if tracked.process_name.lower() not in browsers:
            return
        last_paint = getattr(self, "_overlay_last_paint_monotonic", None)
        if last_paint is None:
            return
        # wh-overlay-fixqueue-review.1: the back-off multiplier stretches the
        # trust window after failed proactive refreshes (a failed refresh
        # restores the prior snapshot by re-entering PAINTED, which re-stamps
        # last_paint -- so the age below measures from the failed attempt).
        backoff = max(
            int(getattr(self, "_overlay_browser_refresh_backoff", 1)), 1,
        )
        if self._overlay_now_monotonic() - last_paint < window_s * backoff:
            return
        # Mark the apply so the REFRESH_IN_FLIGHT entry edge in
        # _apply_overlay_event attributes this refresh to the proactive
        # trigger (back-off and renumber-guard bookkeeping key off it).
        self._overlay_in_proactive_apply = True
        try:
            self._apply_overlay_event(
                OverlayEvent(kind=OverlayEventKind.FOCUS_CHANGE),
                source="browser proactive refresh (keepalive tick)",
            )
        finally:
            self._overlay_in_proactive_apply = False

    def _reconcile_overlay_browser_refresh_reset(self) -> None:
        """Reset the proactive-refresh back-off and renumber guard while CLOSED.

        wh-overlay-fixqueue-review.1/.2: a fresh overlay session starts with
        a fresh proactive-refresh budget and no renumber guard. Level-triggered
        and idempotent (the reconciler idiom): it reads the CURRENT machine
        state, so it is correct from BOTH apply paths -- hide_numbers goes
        through handle_overlay_command, which bypasses _apply_overlay_event.
        """
        from services.wheelhouse.click_overlay_state import OverlayState

        machine = getattr(self, "click_overlay_state", None)
        if machine is None or machine.state is OverlayState.CLOSED:
            self._overlay_browser_refresh_backoff = 1
            self._overlay_proactive_swap = None

    def _overlay_renumber_click_safe(
        self, parsed_number, current_snapshot_id,
    ) -> bool:
        """Check a "click N" against the renumber guard; True means proceed.

        wh-overlay-fixqueue-review.2: for ``_OVERLAY_RENUMBER_GRACE_SECONDS``
        after a PROACTIVE refresh swap, a "click N" may have been spoken
        against the PRE-swap badges. Resolving it against the renumbered
        overlay would click the wrong control with fresh bounds -- the
        stale-position refusal that protected this case before the proactive
        refresh existed never fires. Compare badge N's identity across the
        swap (the prior summary is still in the Logic cache); block only when
        it changed. One block per swap: the notice tells the user to
        re-check, so their next utterance is informed and the guard is
        consumed either way once it decides or expires.
        """
        swap = getattr(self, "_overlay_proactive_swap", None)
        if swap is None:
            return True
        prior_id, swap_t = swap
        if (
            self._overlay_now_monotonic() - swap_t
            > _OVERLAY_RENUMBER_GRACE_SECONDS
        ):
            self._overlay_proactive_swap = None
            return True
        from services.wheelhouse.speech.overlay_click_router import (
            renumber_click_is_safe,
        )

        cache = self.click_snapshot_summary_cache
        prior = cache.resolve(prior_id).summary if prior_id else None
        current = (
            cache.resolve(current_snapshot_id).summary
            if current_snapshot_id else None
        )
        if renumber_click_is_safe(prior, current, parsed_number):
            return True
        self._overlay_proactive_swap = None
        return False

    def _reconcile_overlay_destroy_hook(self, should_be_active: bool) -> None:
        """Register/unregister the transient destroy hook to match paused-ness.

        The destroy hook is registered only while the overlay is ``paused`` and
        is scoped to the tracked window's pid/tid (so the OS does not wake the
        Logic loop for every foreign-process child destroy). Idempotent: it
        only acts on a state edge, so repeated calls in the same paused-ness do
        nothing. Skips registration when no tracked identity is known (no
        pid/tid to scope the hook).
        """
        manager = self._overlay_focus_hooks
        if manager is None:
            return
        if should_be_active and not self._overlay_destroy_hook_active:
            tracked = self._overlay_tracked_identity
            if tracked is None or tracked.hwnd <= 0:
                logger.debug(
                    "overlay: destroy hook not registered (no tracked window "
                    "identity to scope it).",
                )
                return
            pid, tid = self._overlay_window_pid_tid(tracked.hwnd)
            if pid <= 0 or tid <= 0:
                logger.debug(
                    "overlay: destroy hook not registered (could not resolve "
                    "pid/tid for tracked hwnd=%s).", tracked.hwnd,
                )
                return
            # wh-n29v.23.3: only record the hook as active when the register
            # request was ACCEPTED (posted to the hook thread). If the post was
            # rejected (manager not alive, window gone, PostMessage failed), the
            # flag stays False so the next paused reconcile retries instead of
            # believing a dead hook is live.
            if manager.register_destroy_hook(pid=pid, tid=tid):
                self._overlay_destroy_hook_active = True
                logger.info(
                    "overlay: transient destroy hook register requested for "
                    "tracked hwnd=%s (pid=%s tid=%s).", tracked.hwnd, pid, tid,
                )
            else:
                logger.debug(
                    "overlay: destroy hook register request not accepted; will "
                    "retry on the next paused reconcile.",
                )
        elif not should_be_active and self._overlay_destroy_hook_active:
            manager.unregister_destroy_hook()
            self._overlay_destroy_hook_active = False
            logger.info("overlay: transient destroy hook unregistered.")

    def overlay_snapshot_is_valid_on_resume(self) -> bool:
        """Resume-time FULL foreground-identity check (r2.8).

        Returns True iff a tracked identity exists AND the CURRENT foreground
        identity matches it on ALL of HWND + PID + process name + window
        creation time -- the same full-identity rule
        ``ElementFinder.get_snapshot`` enforces, NOT ``IsWindow`` alone. This
        is the value the mic-resume wiring feeds as ``OverlayEvent.snapshot_valid``
        to drive the ``paused`` machine to restore (match) vs re-walk
        (mismatch). It catches the HWND-reuse trap (a recycled HWND on a
        different process) that ``IsWindow`` would miss. With no tracked
        identity there is nothing to restore, so it returns False (re-walk).
        """
        from services.wheelhouse.overlay_focus_hooks import identity_matches

        tracked = self._overlay_tracked_identity
        if tracked is None:
            return False
        current = self._capture_overlay_foreground_identity()
        if current is None:
            return False
        return identity_matches(tracked, current)

    def _overlay_refresh_visible_window_is_foreground(self, machine):
        """Whether a REFRESH_IN_FLIGHT overlay's VISIBLE snapshot is foreground.

        Trigger B (wh-overlay-snapshot-keepalive). Returns:
          * ``True``  -- the visible snapshot's window IS the current foreground
            (a same-window content refresh) -> route normally.
          * ``False`` -- the visible snapshot belongs to a window that is no
            longer foreground (a focus-change refresh) -> the caller passes this
            to ``route_click_n`` so a "click N" HOLDS instead of dispatching a
            click Input would reject on a foreground-identity mismatch.
          * ``None``  -- undeterminable (no recorded identity for the visible
            snapshot, or the current foreground cannot be sampled) -> the router
            keeps its resolve-against-visible default.

        The VISIBLE snapshot is the prior pin when a refresh build already pinned
        a new not-yet-painted snapshot (deferred unpin), else the current pin --
        the same rule ``route_click_n`` uses, so they agree on which snapshot the
        relationship is computed for.
        """
        from services.wheelhouse.overlay_focus_hooks import identity_matches

        visible_id = (
            machine.prior_pinned_snapshot_id if machine.prior_pin_deferred
            else machine.pinned_snapshot_id
        )
        if not visible_id:
            return None
        recorded = self._overlay_snapshot_window_identity.get(visible_id)
        if recorded is None:
            return None
        current = self._capture_overlay_foreground_identity()
        if current is None:
            return None
        return identity_matches(recorded, current)

    def _mint_overlay_trace_id(self) -> str:
        """Mint a click-scoped trace id (mirrors the click_element id shape)."""
        import uuid

        return f"click-{uuid.uuid4().hex[:12]}"

    def _capture_overlay_foreground_identity(self):
        """Sample the CURRENT foreground identity (HWND+PID+name+creation time).

        Thin Win32 seam (lazy win32 imports). Returns a ``ForegroundIdentity``,
        or ``None`` when the foreground window cannot be resolved (so the
        resume check fails closed to re-walk). Read failures degrade to safe
        sentinels per field, mirroring the Input-side ``_capture_click_foreground``
        rule (a real window is required before trusting the pid, so
        ``GetWindowThreadProcessId(0)`` cannot misreport WheelHouse's own pid).
        """
        from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity

        try:
            import win32gui
            import win32process
        except Exception:  # noqa: BLE001 -- non-Windows / missing pywin32
            return None
        hwnd = 0
        pid = 0
        process_name = ""
        creation_time = 0
        try:
            hwnd = int(win32gui.GetForegroundWindow())
        except Exception:  # noqa: BLE001
            hwnd = 0
        if not hwnd:
            return None
        try:
            _thread, raw_pid = win32process.GetWindowThreadProcessId(hwnd)
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
        return ForegroundIdentity(
            hwnd=hwnd,
            pid=pid,
            process_name=process_name,
            window_creation_time=creation_time,
        )

    def _overlay_window_pid_tid(self, hwnd: int) -> tuple[int, int]:
        """Resolve (pid, tid) for ``hwnd`` to scope the transient destroy hook.

        Returns ``(0, 0)`` on any failure so the caller skips registration
        rather than registering a system-wide destroy hook.
        """
        try:
            import win32process

            tid, pid = win32process.GetWindowThreadProcessId(int(hwnd))
            return (int(pid) if pid else 0, int(tid) if tid else 0)
        except Exception:  # noqa: BLE001
            return (0, 0)

    async def _handle_snapshot_item_clicked(self, command) -> None:
        """Resolve a Phase 1.5 numbered-overlay click (wh-jfavj / wh-g4oma).

        The GUI emits ``snapshot_item_clicked { snapshot_id, display_number }``
        on the commands_to_logic_queue when the user clicks a numbered overlay
        item. This handler:

          1. Validates the payload via :func:`safe_parse`. A malformed payload
             (``SnapshotItemClickedSchemaError`` -> ``ValueError``) is logged
             and dropped, per wh-uf54, so a version-skewed sender cannot crash
             the GUI command listener.
          2. Generates a click-scoped ``trace_id`` (mirrors
             ``ActionFunctions.click_element`` / ``forward_click_element``) so
             the resolve + notice log lines correlate.
          3. Resolves (snapshot_id, display_number) via
             ``resolve_display_number`` against the retained
             ``click_snapshot_summary_cache`` and maps the outcome:

               * ``SNAPSHOT_EXPIRED`` -> an
                 ``execution_failed:snapshot_expired`` notice ("the numbered
                 overlay has expired -- say the click command again"; wording
                 owned by wh-g4oma, which needs no spoken_name for this tag).
               * ``NOT_FOUND`` -> COLLAPSED into the identical snapshot_expired
                 notice. The resolver's docstring permits the caller to
                 collapse NOT_FOUND into the snapshot_expired surface: a
                 NOT_FOUND means the user spoke a number not in the live
                 overlay (stale / out-of-range), whose user remedy is identical
                 to expiry (say the command again for fresh numbers), and no
                 separate wording exists -- so reusing snapshot_expired is
                 correct and avoids a silent no-feedback path.
               * ``FOUND`` -> OUT OF SCOPE here. The actual overlay-item click
                 requires the Input-side ``click_snapshot_item`` handler, which
                 is still a Phase 1.5 ``not_implemented`` stub. This handler
                 attempts no click and emits no notice on FOUND; the
                 overlay-click execution slice will wire the dispatch.
        """
        import uuid

        # Import via the full services.wheelhouse.* package path -- the same
        # path main.py's module-level imports and the cache init in __init__
        # (self.click_snapshot_summary_cache) use. resolve_display_number
        # compares the cache's CacheStatus.HIT by identity (`is`), and Python
        # loads this file as a DISTINCT module object under the bare name
        # `click_snapshot_summary_cache` versus the package name. A bare import
        # here would give a different CacheStatus enum object than the
        # package-path cache instance carries, so a real cache HIT would fail
        # the identity check and misresolve as snapshot_expired (wh-9f3t.70.1).
        from services.wheelhouse.shared.ipc_schema_validation import safe_parse
        from services.wheelhouse.shared.snapshot_item_clicked import (
            SnapshotItemClickedEvent,
        )
        from services.wheelhouse.click_snapshot_summary_cache import (
            ResolveOutcome,
            resolve_display_number,
        )

        ev = safe_parse(
            SnapshotItemClickedEvent.from_dict,
            command,
            log_label="snapshot_item_clicked",
        )
        if ev is None:
            return  # already logged

        # Mint a click-scoped trace_id (the GUI->Logic event carries none),
        # mirroring ActionFunctions.click_element's id shape so the resolve
        # and notice log lines correlate with the rest of the click flow.
        trace_id = f"click-{uuid.uuid4().hex[:12]}"

        # Degrade, do not die. This handler runs as a
        # create_task_with_error_handling background task whose done-callback
        # calls request_shutdown() on any uncaught exception. An advisory
        # click notice must never be able to restart the whole Logic process,
        # so wrap the resolve + forward in the same degrade-don't-die posture
        # forward_click_element uses for its notice surface (wh-9f3t.69.3): an
        # unexpected error here logs and drops the click silently rather than
        # escalating to a process-wide shutdown. safe_parse above already
        # handles the malformed-payload (ValueError) path.
        try:
            result = resolve_display_number(
                self.click_snapshot_summary_cache,
                ev.snapshot_id,
                ev.display_number,
            )

            if result.outcome is ResolveOutcome.FOUND:
                # wh-n29v.95: dispatch the real overlay-item click. The resolver
                # mapped the display number to an item_id in the live retained
                # snapshot; forward it to the Input process via
                # ``click_snapshot_item`` (the GUI-to-Logic round-trip step 6
                # dispatch). The send + ClickElementResponse parse + non-ok
                # click-notice are owned by ``_send_snapshot_item_click`` (the
                # SAME path the voice ``click N`` route uses). No notice on ok.
                logger.info(
                    "snapshot_item_clicked: resolved display_number=%d to "
                    "item_id=%r in snapshot=%r; dispatching click_snapshot_item "
                    "(trace_id=%s)",
                    ev.display_number, result.item_id, ev.snapshot_id,
                    trace_id,
                )
                self._dispatch_snapshot_item_click(
                    snapshot_id=ev.snapshot_id,
                    item_id=result.item_id,
                    trace_id=trace_id,
                )
                return

            # SNAPSHOT_EXPIRED and NOT_FOUND both surface as the shipped
            # snapshot_expired notice. The NOT_FOUND collapse is intentional
            # (see the docstring): same user remedy, no distinct wording.
            if result.outcome is ResolveOutcome.NOT_FOUND:
                logger.info(
                    "snapshot_item_clicked: display_number=%d not in live "
                    "snapshot=%r; collapsing NOT_FOUND into the "
                    "snapshot_expired notice (trace_id=%s)",
                    ev.display_number, ev.snapshot_id, trace_id,
                )
            else:
                logger.info(
                    "snapshot_item_clicked: snapshot=%r expired/missing; "
                    "emitting snapshot_expired notice (trace_id=%s)",
                    ev.snapshot_id, trace_id,
                )

            self._forward_click_notice(
                outcome="execution_failed",
                reason="snapshot_expired",
                matched_name=None,
                matched_names=(),
                # The event carries only snapshot_id + display_number; the
                # snapshot_expired wording does not embed spoken_name.
                spoken_name="",
                snapshot_id=ev.snapshot_id,
                trace_id=trace_id,
            )
        except Exception:
            # Drop the click silently rather than restart the Logic process.
            logger.exception(
                "snapshot_item_clicked: unexpected error resolving / "
                "forwarding the notice for snapshot=%r display_number=%d; "
                "dropping the click (trace_id=%s)",
                ev.snapshot_id, ev.display_number, trace_id,
            )

    def _forward_click_notice(
        self,
        *,
        outcome: str,
        reason: Optional[str],
        matched_name: Optional[str],
        matched_names: tuple,
        spoken_name: str,
        snapshot_id: Optional[str],
        trace_id: str,
    ) -> None:
        """Forward a ClickNoticeEvent to the GUI for a non-ok click outcome.

        Builds the wh-lstwt ClickNoticeEvent payload and puts a
        ``show_click_notice`` action on the GUI state queue. The notice
        WORDING (wh-g4oma) and the GUI render path consume this; this
        method only populates the schema-valid payload, including
        trace_id. app_friendly_name is left empty here (the
        ClickElementResponse does not carry the process identity); the
        wording slice resolves any display name it needs.
        """
        from shared.click_notice import ClickNoticeEvent

        try:
            event = ClickNoticeEvent(
                outcome=outcome,
                reason=reason,
                matched_name=matched_name,
                matched_names=tuple(matched_names),
                spoken_name=spoken_name,
                app_friendly_name="",
                snapshot_id=snapshot_id,
                trace_id=trace_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "click_element: failed to build ClickNoticeEvent "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return

        if not self.state_manager or not hasattr(
            self.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "click_element notice dropped, no state_manager queue "
                "(trace_id=%s)", trace_id,
            )
            return

        gui_msg = {"action": "show_click_notice", **event.to_dict()}
        try:
            self.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "click_element notice dropped, GUI queue put failed "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return
        # wh-n29v.122: the success path used to write ZERO log lines (both
        # logs above are failure-only), so a live session could not tell
        # "notice never sent" from "sent and missed while the toast
        # auto-dismissed". One INFO line after the successful put; the GUI
        # logs its own line after rendering, sharing this trace_id.
        logger.info(
            "click notice forwarded to GUI: outcome=%s reason=%s matched=%r "
            "(trace_id=%s)", outcome, reason, matched_name, trace_id,
        )

    def _resolve_first_use_hint_path(self) -> "Path":
        """Return the path to the first-use-hint record file (wh-r3xy1).

        Tests override the location by setting ``self._first_use_hint_path``
        on the controller before the first click. Production resolves to the
        same default the writer uses.
        """
        override = getattr(self, "_first_use_hint_path", None)
        if override is not None:
            return override
        from services.wheelhouse.click_first_use_hint import default_hint_path

        return default_hint_path()

    def _first_use_hint_tracker(self) -> "FirstUseHintTracker | None":
        """Lazily build (and memoise) the first-use hint tracker (wh-r3xy1).

        Built on first use so a session that never issues a click -- or a test
        that does not exercise the hint -- never reads the record file. Returns
        None if construction fails for any reason; a hint problem must never
        break the click flow.
        """
        if self._first_use_hint is not None:
            return self._first_use_hint
        try:
            from services.wheelhouse.click_first_use_hint import (
                FirstUseHintTracker,
            )

            # Match the predicate's resolved browser-process view: the
            # ClickConfig starter list plus any user extension. is_chromium_
            # family lower-cases internally so casing in config is fine.
            browser_names = (
                tuple(self.click_config.browser_processes)
                + tuple(self.click_config.browser_processes_extend)
            )
            self._first_use_hint = FirstUseHintTracker(
                self._resolve_first_use_hint_path(),
                browser_process_names=browser_names,
            )
        except Exception as exc:  # noqa: BLE001 -- never break the click flow
            logger.warning(
                "click_first_use_hint: failed to build tracker: %s", exc,
            )
            return None
        return self._first_use_hint

    def _resolve_foreground_process_name(self) -> str:
        """Best-effort exe basename of the current foreground window (wh-r3xy1).

        The ``click_element`` round trip does not carry the target process
        name back to Logic, and the spoken ``ElementQuery`` does not carry it
        either, so the hint resolves the foreground process directly. This is
        a read-only observation; it does not touch the click execution path.
        Returns ``""`` on any failure (no foreground, access denied, missing
        Win32 dependency) so the caller simply skips the hint.
        """
        try:
            import psutil
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return ""
            return psutil.Process(pid).name()
        except Exception as exc:  # noqa: BLE001 -- never break the click flow
            logger.debug(
                "click_first_use_hint: could not resolve foreground "
                "process: %s", exc,
            )
            return ""

    async def _maybe_show_first_use_hint(self, trace_id: str) -> None:
        """Surface the screen-reader-flag first-use hint when eligible (wh-r3xy1).

        Resolves the foreground process, consults the suppression tracker, and
        on a positive verdict pushes a ``click_first_use_hint`` action onto the
        existing GUI state queue. Never raises; any failure logs and returns so
        the click flow is unaffected.

        Latency (wh-9f3t.60.4): the foreground resolution does three blocking
        Win32 / psutil calls, so it is offloaded to a worker thread via
        ``asyncio.to_thread`` and never runs on the Logic asyncio loop. The
        ``recorded_shown`` short-circuit means neither the offload nor the
        tracker work runs once the hint has been recorded. The cheap tracker
        bookkeeping (and the rare, at-most-once ``os.fsync`` inside
        ``_record_shown`` it may trigger) stays on the loop; the fsync fires at
        most once per machine for the whole feature lifetime, so the ordering
        simplicity of keeping it inline outweighs offloading it.

        Delivery-gated display (wh-9f3t.61.2): the decision is split from the
        state mutation. ``tracker.evaluate`` is a pure verdict; on a SHOW
        verdict the GUI action is forwarded FIRST and the display is committed
        (``commit_displayed``) ONLY if the enqueue succeeded. If the GUI queue
        is absent or the put fails, the tracker is left unmutated so the next
        eligible click retries the display -- a hint the user never saw is
        never recorded as shown. A COUNT verdict advances the persistence
        counter via ``note_counted``.
        """
        try:
            from services.wheelhouse.click_first_use_hint import HintDecision

            tracker = self._first_use_hint_tracker()
            if tracker is None:
                return
            if tracker.recorded_shown:
                return
            process_name = await asyncio.to_thread(
                self._resolve_foreground_process_name
            )
            if not process_name:
                return
            decision = tracker.evaluate(
                process_name,
                flag_enabled=self.click_config.enable_screen_reader_flag,
            )
            if decision is HintDecision.SHOW:
                # Forward FIRST; commit the one-shot display only on a
                # successful enqueue so a failed delivery retries next click.
                if self._forward_first_use_hint(trace_id):
                    tracker.commit_displayed()
            elif decision is HintDecision.COUNT:
                tracker.note_counted()
        except Exception as exc:  # noqa: BLE001 -- never break the click flow
            logger.warning(
                "click_first_use_hint: hint hook failed (trace_id=%s): %s",
                trace_id, exc,
            )

    def _forward_first_use_hint(self, trace_id: str) -> bool:
        """Push the ``click_first_use_hint`` action to the GUI (wh-r3xy1).

        Rides the existing GUI state queue (the same queue carrying
        ``show_click_notice`` / ``show_rejection_toast``); no new IPC channel.
        The GUI renders ``HINT_TEXT`` via the existing info-notice path.

        Returns True iff the action was actually enqueued. The caller commits
        the tracker's one-shot display only on True so a missing-queue or
        failed-put does not burn the display (wh-9f3t.61.2).
        """
        from services.wheelhouse.click_first_use_hint import HINT_TEXT

        if not self.state_manager or not hasattr(
            self.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "click_first_use_hint dropped, no state_manager queue "
                "(trace_id=%s)", trace_id,
            )
            return False

        gui_msg = {
            "action": "click_first_use_hint",
            "message": HINT_TEXT,
            "trace_id": trace_id,
        }
        try:
            self.state_manager.state_to_gui_queue.put_nowait(gui_msg)
            logger.info(
                "click_first_use_hint: surfaced first-use hint "
                "(trace_id=%s)", trace_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "click_first_use_hint dropped, GUI queue put failed "
                "(trace_id=%s): %s", trace_id, exc,
            )
            return False

    async def _handle_grant_prompt_no_clicked(self, command: dict) -> None:
        """Handle a No click on the three-strikes grant prompt (wh-vdt1t / wh-27gvv).

        Validates the payload, then persists the identity tuple via
        ``add_declined`` so the No choice survives a restart (wh-27gvv).
        ``add_declined`` writes to disk first and on success updates
        ``_grant_prompt_no_suppressed`` so subsequent
        ``RetryThresholdReached`` events for the same tuple drop their
        forward at ``_on_retry_threshold_reached`` instead of re-firing
        the approval prompt. On a disk-write failure ``add_declined``
        enqueues a ``declined_write_failed`` action on the GUI state
        queue and the in-memory set is left untouched; the approval
        prompt re-fires on the next verified-retry threshold so the
        user can decline again.

        The counter is intentionally NOT reset (per bead spec wh-vdt1t):
        the next verified retry still increments, but the follow-up
        approval prompt does not re-fire because the forwarder
        consults the suppression set first.

        Stale-forward race (wh-vbvgf.13.2, accepted tradeoff): if a
        ``RetryVerified`` for the same tuple fires while the No-click
        IPC is still queued on commands_to_logic_queue, the
        ``RetryThresholdReached`` publish chain runs to completion
        before the No handler runs and updates the suppression set.
        The forwarder sees the empty set, sends a fresh
        ``text_target_grant_prompt`` to the GUI, and the approval
        prompt may re-appear briefly. The No handler runs on the next
        event-loop tick, suppression takes effect, and all subsequent
        forwards for that tuple drop. The visible symptom is a
        one-tick flicker; no data loss, no stuck state. The
        alternatives -- GUI-side dedup (reintroduces wh-vbvgf.12.1)
        or serialising EventBus behind queue processing (adds latency
        to the hot path) -- are worse than the flicker. Documenting
        the accepted tradeoff here so a future maintainer does not
        re-investigate.

        Failure handling: a malformed payload is logged via
        ``safe_parse`` and dropped (per wh-uf54). A disk-write failure
        is surfaced via the ``declined_write_failed`` action that
        ``add_declined`` enqueues.

        Privacy: the handler reads exactly the three identity fields
        from the payload. Any additional keys a future buggy sender
        might add are ignored because the schema validator does not
        include them in the parsed event.
        """

        from shared.ipc_schema_validation import safe_parse
        from shared.grant_prompt_no_clicked import GrantPromptNoClickedEvent

        event = safe_parse(
            GrantPromptNoClickedEvent.from_dict,
            command,
            log_label="grant_prompt_no_clicked",
        )
        if event is None:
            return  # already logged

        tuple_key = (
            event.process_name, event.class_name, event.control_type,
        )
        # Symmetric with the Yes path's wrapper around add_soft_allow:
        # the GUI command listener is not allowed to crash because of
        # a buggy persistence path. add_declined is documented as
        # not-raising, but a future writer change or an exotic input
        # could leak an exception; this wrapper contains it.
        try:
            ok = await self.add_declined(
                event.process_name, event.class_name, event.control_type,
            )
        except Exception as exc:
            logger.warning(
                "grant_prompt_no_clicked: add_declined raised for "
                "tuple=%s: %s -- suppression NOT applied",
                tuple_key, exc,
                exc_info=True,
            )
            return

        if ok:
            logger.info(
                "grant_prompt_no_clicked: tuple=%s persisted and suppressed",
                tuple_key,
            )
        else:
            logger.warning(
                "grant_prompt_no_clicked: tuple=%s write failed; "
                "suppression NOT applied -- the approval prompt will "
                "re-fire on the next verified-retry threshold",
                tuple_key,
            )

    async def _handle_grant_prompt_yes_clicked(self, command: dict) -> None:
        """Handle a Yes click on the three-strikes grant prompt (wh-8d81z).

        Validates the payload, then calls
        :meth:`add_soft_allow` (which writes the soft-allow file and on
        disk success sends ``add_soft_allow_tuple`` IPC to the input
        process). On full success the click counter for the tuple is
        reset to zero so a future de-grant by the user does not
        immediately re-fire the threshold prompt; on disk-write failure
        the counter is intentionally NOT reset (per bead spec wh-8d81z)
        so the user can click Yes again later.

        Failure handling:
          * A malformed payload is logged via ``safe_parse`` and dropped.
          * An exception inside ``add_soft_allow`` is caught, logged,
            and the counter is left as-is. The GUI command listener is
            not allowed to crash because of a buggy persistence path.
          * The ``soft_allow_write_failed`` event GUI surfacing is the
            existing wh-9dkse path inside ``add_soft_allow``.

        Privacy: the handler reads exactly the three identity fields
        from the payload and forwards them to ``add_soft_allow``. Any
        additional keys a future buggy sender might add are ignored
        because the schema validator does not include them in the
        parsed event.
        """

        from shared.ipc_schema_validation import safe_parse
        from shared.grant_prompt_yes_clicked import GrantPromptYesClickedEvent

        event = safe_parse(
            GrantPromptYesClickedEvent.from_dict,
            command,
            log_label="grant_prompt_yes_clicked",
        )
        if event is None:
            return  # already logged

        try:
            outcome = await self.add_soft_allow(
                event.process_name,
                event.class_name,
                event.control_type,
            )
        except Exception as exc:
            logger.warning(
                "grant_prompt_yes_clicked: add_soft_allow raised for "
                "tuple=(%s, %s, %s): %s -- counter not reset",
                event.process_name, event.class_name, event.control_type, exc,
                exc_info=True,
            )
            return

        if not outcome.is_durable:
            # Disk write failed; add_soft_allow already enqueued the
            # soft_allow_write_failed event. Counter stays so the user
            # can retry Yes later (per wh-8d81z spec).
            return

        # wh-vbvgf.9.2 (codex review): reset on SUCCESS or IPC_FAILED.
        # The soft-allow tuple is durable on disk in both cases; the
        # input process picks it up on the next launcher run, so the
        # counter is no longer the right thing to keep around.
        #
        # wh-reset-race-concurrent-verified (deepseek review): guard
        # against the race where _on_retry_verified for the same tuple
        # acquires the per-tuple asyncio.Lock just after reset_tuple
        # released it. The verify path runs `count = get(key, 0) + 1`
        # and writes the counter back at 1, leaving an orphan entry
        # the user would only notice after a manual de-grant. After
        # reset returns, we check the count and retry once if a
        # concurrent increment slipped in. The race window closes
        # once the input process has applied the add_soft_allow_tuple
        # IPC (after which no further rejections fire for the tuple
        # and no further RetryVerified events can land), so one retry
        # is enough in practice.
        try:
            await self.click_counter.reset_tuple(
                event.process_name,
                event.class_name,
                event.control_type,
            )
            if self.click_counter.get_count(
                event.process_name,
                event.class_name,
                event.control_type,
            ) != 0:
                logger.info(
                    "grant_prompt_yes_clicked: concurrent retry verified "
                    "after reset for tuple=(%s, %s, %s); resetting again",
                    event.process_name, event.class_name, event.control_type,
                )
                await self.click_counter.reset_tuple(
                    event.process_name,
                    event.class_name,
                    event.control_type,
                )
        except Exception as exc:
            logger.warning(
                "grant_prompt_yes_clicked: counter reset failed for "
                "tuple=(%s, %s, %s): %s -- soft-allow grant succeeded",
                event.process_name, event.class_name, event.control_type, exc,
                exc_info=True,
            )

    async def _handle_try_anyway_clicked(self, command: dict) -> None:
        """Resolve a Try-it-anyway click and dispatch the retry (wh-iycks).

        The GUI emits ``try_anyway_clicked { correlation_token }`` over
        the existing GUI-to-Logic queue when the user clicks the
        Try-it-anyway button on a rejection toast (wh-z7qx1). This
        handler:

          1. Validates the payload via :func:`safe_parse`. A malformed
             payload is logged and dropped (per wh-uf54) so a
             version-skewed sender cannot crash the GUI command
             listener.
          2. Resolves the correlation_token in
             ``self.rejection_token_cache``:
               * HIT     -> awaits ``forward_retry_dictation_by_token``,
                            which sends the actual ``retry_dictation_by_token``
                            request to the Input process.
               * EXPIRED or MISS -> emits a ``click_too_late`` INFO log
                            line carrying ONLY the token (privacy: no
                            dictation text), then synthesises the
                            canonical follow-up toast. No IPC to Input
                            and no counter increment, per the bead spec.

        Privacy: this handler never sees dictation text. Logic owns
        only the correlation_token and the token -> tuple cache. The
        text lives only in the Input process.
        """

        from shared.ipc_schema_validation import safe_parse
        from shared.try_anyway_clicked import TryAnywayClickedEvent
        from shared.rejection_token_cache import CacheStatus

        event = safe_parse(
            TryAnywayClickedEvent.from_dict,
            command,
            log_label="try_anyway_clicked",
        )
        if event is None:
            return  # already logged

        result = self.rejection_token_cache.resolve(event.correlation_token)
        if result.status is CacheStatus.HIT:
            # wh-vbvgf.3.2: pass the resolved tuple through so the
            # publish decision after the IPC round trip is anchored on
            # the click that was accepted, not on a second wall-clock
            # cache lookup that can race with TTL expiry on a slow
            # paste path.
            await self.forward_retry_dictation_by_token(
                event.correlation_token,
                rejection=result.tuple_,
            )
            return

        # EXPIRED or MISS: log click_too_late at INFO with ONLY the
        # token (privacy contract -- no dictation text), then surface
        # the canonical follow-up toast.
        logger.info(
            "click_too_late: try_anyway_clicked for token=%s "
            "resolved as %s; emitting follow-up toast",
            event.correlation_token, result.status.value,
        )
        self._send_retry_followup_toast()

    async def forward_retry_dictation_by_token(
        self, correlation_token: str,
        rejection: Optional[RejectionTuple] = None,
    ) -> None:
        """Forward a Try-it-anyway click to the input process (wh-ftg63).

        The GUI emits ``try_anyway_clicked`` with the correlation_token
        from the rejection toast (the GUI emit is wh-iycks, out of scope
        here). This handler:

          1. Sends a ``retry_dictation_by_token`` request to the input
             process via ``WheelHouseApp.send_request``. The request
             carries ONLY the correlation_token and override_strategy --
             no dictation text crosses processes.
          2. Awaits the response. The input process resolves the
             correlation_token in its rejection-text cache and either
             runs ClipboardOnlyStrategy (status=success) or returns
             ``token_expired`` / ``unknown_token``.
          3. On non-success, forwards a one-line follow-up toast to the
             GUI via the existing ``show_notification`` action. The
             wording is the bead-spec contract (`EXPECTED_WORDING` in
             tests). Both ``token_expired`` and ``unknown_token`` use
             the same wording -- the distinction is for log surfaces.
          4. On success, returns silently. The verified-retry counter
             increment is wh-mv5ih territory and out of scope here.

        Failure handling: ``send_request`` may time out or raise; the
        try/except logs at WARNING and degrades to the same follow-up
        toast so a stuck input process does not strand the user with
        no feedback. A malformed response (missing status, etc.) is
        likewise treated as failure.

        Privacy: this method NEVER sees the dictation text. Logic owns
        only the correlation_token and (in Phase 4 phases that are not
        in this bead) a token -> tuple cache. The text lives only in
        the input process.
        """
        from services.wheelhouse.shared.retry_dictation_by_token import (
            ACTION_NAME,
            OVERRIDE_CLIPBOARD_ONLY,
            RetryDictationByTokenResponse,
            RetryDictationByTokenSchemaError,
            STATUS_SUCCESS,
        )

        # wh-82lnx.2.2: token reservation BEFORE the IPC round trip.
        # Two concurrent clicks for the same correlation_token must
        # not both reach send_request and trigger duplicate clipboard
        # pastes into the focused control. consumed_retry_tokens
        # gates against post-success replays; _in_flight_retry_tokens
        # gates against concurrent in-flight callers.
        if correlation_token in self.consumed_retry_tokens:
            logger.debug(
                "duplicate try_anyway click for token %s (already consumed); "
                "ignoring",
                correlation_token,
            )
            return
        if correlation_token in self._in_flight_retry_tokens:
            logger.debug(
                "concurrent try_anyway click for token %s (already in flight); "
                "ignoring",
                correlation_token,
            )
            return
        self._in_flight_retry_tokens.add(correlation_token)

        params = {
            "correlation_token": correlation_token,
            "override_strategy": OVERRIDE_CLIPBOARD_ONLY,
        }

        try:
            try:
                raw_response = await self.app.send_request(ACTION_NAME, params)
            except asyncio.TimeoutError:
                logger.warning(
                    "retry_dictation_by_token: request timed out for token=%s; "
                    "surfacing follow-up toast",
                    correlation_token,
                )
                self._send_retry_followup_toast()
                return
            except Exception as exc:
                logger.warning(
                    "retry_dictation_by_token: send_request failed for "
                    "token=%s: %s",
                    correlation_token, exc,
                )
                self._send_retry_followup_toast()
                return

            try:
                response = RetryDictationByTokenResponse.from_dict(raw_response)
            except RetryDictationByTokenSchemaError as exc:
                logger.warning(
                    "retry_dictation_by_token: malformed response for "
                    "token=%s: %s",
                    correlation_token, exc,
                )
                self._send_retry_followup_toast()
                return

            if response.status == STATUS_SUCCESS:
                logger.debug(
                    "retry_dictation_by_token: success outcome=%s for token=%s",
                    response.retry_outcome, correlation_token,
                )
                # wh-mv5ih: only verified outcomes count toward the click
                # counter. unverified means the paste ran but the strategy
                # could not confirm any text landed; it must NOT advance
                # the counter (the user can keep clicking).
                from services.wheelhouse.shared.retry_dictation_by_token import (
                    RETRY_OUTCOME_VERIFIED,
                )
                if response.retry_outcome != RETRY_OUTCOME_VERIFIED:
                    return
                # Prefer the rejection tuple captured at click time
                # (wh-vbvgf.3.2). _handle_try_anyway_clicked resolves the
                # token before dispatching the IPC and passes the tuple
                # through, so a TTL expiry during the round trip cannot
                # erase a verified retry. The cache fallback covers older
                # callers that did not pass the tuple; both paths fail
                # closed if no tuple is available, since the future counter
                # keys off the tuple.
                if rejection is None:
                    rejection = self.rejection_token_cache.get(correlation_token)
                if rejection is None:
                    logger.debug(
                        "retry_dictation_by_token: verified outcome but "
                        "no rejection tuple available for correlation_token=%s "
                        "(no caller-supplied tuple, cache lookup also missed); "
                        "skipping RetryVerified publish",
                        correlation_token,
                    )
                    return
                # wh-82lnx.2.2: token reservation at the top of this
                # method already gated against duplicate concurrent
                # clicks. Mark the verified click as consumed before
                # publishing so a future click after the in-flight set
                # has cleared still hits the consumed-set short-circuit.
                self.consumed_retry_tokens.add(correlation_token)
                try:
                    await self.event_bus.publish(RetryVerified(
                        process_name=rejection.process_name,
                        class_name=rejection.class_name,
                        control_type=rejection.control_type,
                        app_friendly_name=rejection.app_friendly_name,
                    ))
                except Exception as exc:
                    # EventBus.publish already isolates handler failures;
                    # this guard catches any exception from publish itself
                    # so a bus-level bug does not abort the retry forwarder.
                    logger.warning(
                        "retry_dictation_by_token: RetryVerified publish "
                        "failed: %s",
                        exc,
                    )
                return

            # token_expired or unknown_token: same user-facing follow-up.
            logger.debug(
                "retry_dictation_by_token: non-success status=%s for token=%s "
                "(reason=%s)",
                response.status, correlation_token, response.reason or "-",
            )
            self._send_retry_followup_toast()
        finally:
            # wh-82lnx.2.2: release the in-flight reservation on every
            # exit path. Verified successes are also tracked in
            # consumed_retry_tokens so a post-success replay still gets
            # the duplicate-click drop. Non-verified or failure paths
            # leave the user free to retry the same toast click after
            # the network/IPC issue resolves.
            self._in_flight_retry_tokens.discard(correlation_token)

    def _send_retry_followup_toast(self) -> None:
        """Push a one-line follow-up toast onto the GUI state queue.

        Uses the existing ``show_notification`` action that gui.py
        already handles (see action == "show_notification" in
        gui.py around line 578); we deliberately do NOT add a new IPC
        event for this in wh-ftg63. The GUI side that wires this into
        the rejection-toast widget (rather than a system tray
        notification) is the responsibility of wh-iycks / wh-9dkse;
        when those land, this caller can switch to the new action
        without changing the wording contract.
        """
        if not self.state_manager or not hasattr(
            self.state_manager, "state_to_gui_queue",
        ):
            logger.warning(
                "retry_dictation_by_token: cannot surface follow-up toast, "
                "no state_manager.state_to_gui_queue available"
            )
            return

        message = (
            "WheelHouse couldn't try again. Say the words again, "
            "then click Try it anyway."
        )
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "show_notification",
                "title": "WheelHouse",
                "message": message,
                "timeout": 5,
            })
        except Exception as exc:
            logger.warning(
                "retry_dictation_by_token: follow-up toast enqueue failed: %s",
                exc,
            )

    def _forward_te_event_to_gui(self, msg: dict):
        """Forward terminal editor event from Input to GUI Process."""
        event = msg.get("event")
        gui_msg = {"action": f"te_{event}"}

        if event == "show":
            gui_msg["text"] = msg.get("text", "")
            gui_msg["hwnd"] = msg.get("hwnd", 0)
            gui_msg["rect"] = msg.get("rect", ())
            # wh-t81d9.2: thread the proxy-generated request_id through to GUI
            # so the ack carries it back unchanged.
            gui_msg["request_id"] = msg.get("request_id", "")
        # submit and cancel need no extra fields

        self.state_manager.state_to_gui_queue.put_nowait(gui_msg)
        logger.debug("Forwarded te_%s to GUI", event)

    async def _handle_te_cancelled(self):
        """GUI cancelled terminal editor. Notify Input to clear proxy state.

        wh-g2-refactor.18: the bridge into the focus-redirect path is
        gone with the path. Only the input-process proxy notification
        remains.
        """
        logger.debug("Terminal editor cancelled by GUI")
        await self.app.send_command("terminal_editor_cancelled")

    async def _handle_te_event_ack(self, request_id: str, op: str, editor_hwnd: int):
        """Forward a GUI te_event ack to the input-process proxy (wh-t81d9.2).

        The proxy's on_event_ack consumes submit lifecycle acks
        (``submit_complete`` and ``submit_failed:<reason>``) to clear
        ``_is_active`` and ``_submit_in_progress`` and cancel the safety
        timer, and consumes ``show`` acks to record the editor HWND so
        the retract focus check has a real value to compare against.
        Implemented as a control command so the input main loop's
        existing dispatch routes it without changing send_command's
        contract.

        wh-g2-refactor.18: the LogicMirror / EditorLifecycleEvent
        bridge was removed with the focus-redirect path. Only the
        proxy forward remains.
        """
        if not request_id:
            logger.warning("te_event_ack with empty request_id; dropping")
            return
        await self.app.send_command(
            "_te_event_ack",
            {"request_id": request_id, "op": op, "editor_hwnd": editor_hwnd},
        )

    # ------------------------------------------------------------------
    # wh-g2-refactor.18 (Sections 2, 5, 6): per-word insert and retract
    # producers, response handlers, and the editor_rebuilt notification
    # handler. The producers follow the pop-ownership pattern (slice
    # 18.1 / wh-g2-refactor.21.1): put_nowait runs INSIDE the try whose
    # finally pops the pending entry, so a queue-full exception cannot
    # leak a permanent map entry. The response handlers parse every
    # inbound payload through the shared schema's from_dict so a
    # malformed inbound response is rejected at the boundary rather
    # than silently propagated to the producer (slice 18.1 / 21.2).
    # ------------------------------------------------------------------

    def show_editor_persistent(self, terminal_hwnd: int) -> None:
        """Send a ``te_show`` to the GUI for the persistent editor.

        wh-wisp-07m: restored after slice 18 of the G2 refactor deleted
        every producer of ``te_show``. The persistent editor is
        constructed at GUI startup and remains hidden until this call
        reveals it. The focus-redirect policy's redirect decision is
        the trigger; the speech pipeline calls this once per utterance
        before the first ``insert_editor_word`` so the user can see
        the words land and press Enter to submit them to the captured
        terminal.

        Resolves the terminal's screen rect via ``GetWindowRect`` here
        in Logic (a plain Win32 call that does not need UIA) so we
        bypass the Input-process round-trip the legacy
        ``open_editor_for_redirect`` IPC used. A failure to resolve
        the rect is logged and an empty tuple is sent; the GUI's
        ``_setup_geometry`` falls back to a default 500x160 size in
        that case so the editor still appears, just unpositioned.
        """
        rect: tuple = ()
        if terminal_hwnd:
            try:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(
                    int(terminal_hwnd),
                )
                if right > left and bottom > top:
                    rect = (left, top, right, bottom)
            except Exception as exc:
                logger.debug(
                    "show_editor_persistent: GetWindowRect(%s) failed: %s",
                    terminal_hwnd, exc,
                )
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "te_show",
                "text": "",
                "hwnd": int(terminal_hwnd),
                "rect": rect,
                "request_id": "",
                "utterance_id": "",
            })
        except Exception as exc:
            logger.warning(
                "show_editor_persistent: enqueue te_show failed (hwnd=%s): %s",
                terminal_hwnd, exc,
            )

    async def insert_editor_word(self, text: str, utterance_id: str) -> int:
        """Send a per-word insert IPC and await the GUI response.

        Returns the GRAPHEME-CLUSTER count the editor inserted into its
        document (``clusters_inserted`` from the GUI response). Returns ``0``
        on every non-success outcome (empty text, timeout, declined,
        rebuild-fenced). The speech-side caller adds the return value to its
        per-utterance editor total so a later retract -- which peels grapheme
        clusters -- requests the matching span. Clusters, not ``len(text)``
        and not the UTF-16 ``chars_inserted``: the former misses the leading
        space the editor adds to words 2..N, the latter over-counts
        astral-plane input and underruns the retract
        (wh-editor-retract-dup / wh-editor-retract-dup.1.1).
        """
        import uuid

        if not text:
            return 0
        request_id = uuid.uuid4().hex
        generation = self._editor_rebuild_fanout.observed_generation
        future = self._insert_pending.register(
            request_id, generation=generation,
        )
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "insert_editor_word",
                "request_id": request_id,
                "text": text,
                "utterance_id": utterance_id,
                "editor_generation": generation,
            })
            try:
                response = await asyncio.wait_for(
                    future, timeout=self._insert_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "insert_editor_word timed out (rid=%s len=%d)",
                    request_id, len(text),
                )
                return 0
        finally:
            self._insert_pending.pop(request_id)
        reason = response.get("failure_reason", "")
        if reason in ("stale_generation", "editor_rebuilt"):
            logger.info(
                "insert_editor_word fenced by rebuild: %s (rid=%s)",
                reason, request_id,
            )
            return 0
        if reason:
            logger.info(
                "insert_editor_word declined: %s (rid=%s)", reason, request_id,
            )
            return 0
        return int(response.get("clusters_inserted", 0) or 0)

    async def retract_editor_text(
        self,
        chars_requested: int,
        utterance_id: str,
        replay_text: str = "",
        whole_utterance: bool = False,
    ) -> None:
        """Send a retract+replay IPC and await the GUI response.

        See Section 2 of the G2 design doc. Returns silently on every
        outcome; the next STT update heals partial / dropped retracts.
        Honours the chars_requested == -1 abandon-path sentinel that
        ``build_rebuild_lost_payload`` synthesises for the rebuild
        fan-out (wh-g2-refactor.29.3).

        ``whole_utterance=True`` selects the ledger-authoritative mode
        (wh-editor-retract-ledger-authoritative): the GUI peels ALL
        ledger runs for the utterance and ``chars_requested`` is only
        the advisory mirror count -- 0 is legal (a fully-drifted
        mirror), so the <= 0 early-return applies to counted mode only.
        """
        import uuid

        if chars_requested <= 0 and not whole_utterance:
            return
        if chars_requested < 0:
            chars_requested = 0
        request_id = uuid.uuid4().hex
        generation = self._editor_rebuild_fanout.observed_generation
        future = self._retract_pending.register(
            request_id, generation=generation,
        )
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "retract_editor_text",
                "request_id": request_id,
                "chars_requested": chars_requested,
                "utterance_id": utterance_id,
                "replay_text": replay_text,
                "editor_generation": generation,
                "whole_utterance": whole_utterance,
            })
            try:
                response = await asyncio.wait_for(
                    future, timeout=self._retract_timeout_s,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "retract_editor_text timed out (rid=%s)", request_id,
                )
                return
        finally:
            self._retract_pending.pop(request_id)

        failure_reason = response.get("failure_reason", "")
        # wh-g2-refactor.29.3: abandon-path short-circuit must come
        # BEFORE the boundary comparison. The rebuild fan-out uses
        # chars_requested=-1 as the sentinel for "no specific request
        # context"; comparing against the original chars_requested
        # would spuriously log a "mismatch" and miss the rebuild fence.
        if (
            response.get("chars_requested") == -1
            and failure_reason in ("stale_generation", "editor_rebuilt")
        ):
            logger.info(
                "retract_editor_text fenced by rebuild: %s (rid=%s)",
                failure_reason, request_id,
            )
            return
        # Boundary check: the GUI MUST echo the request's chars_requested
        # value (round 1 / codex finding A, wh-g2-refactor.5.1).
        if response.get("chars_requested") != chars_requested:
            logger.warning(
                "retract_editor_text_response chars_requested mismatch "
                "(sent=%d got=%s rid=%s)",
                chars_requested, response.get("chars_requested"), request_id,
            )
            return
        # Same boundary discipline for the whole_utterance echo
        # (wh-editor-retract-ledger-authoritative reviewer_0 finding
        # .1.2): the mode decides which success invariant the schema
        # enforced GUI-side, so a mismatched echo means the two sides
        # validated different contracts.
        if bool(response.get("whole_utterance", False)) != whole_utterance:
            logger.warning(
                "retract_editor_text_response whole_utterance mismatch "
                "(sent=%s got=%s rid=%s)",
                whole_utterance, response.get("whole_utterance"), request_id,
            )
            return
        if failure_reason == "":
            return
        if failure_reason == "ledger_underrun":
            logger.info(
                "retract_editor_text underrun: removed %d of %d (rid=%s); "
                "not replaying",
                response.get("chars_removed", 0), chars_requested, request_id,
            )
            return
        if failure_reason in ("stale_generation", "editor_rebuilt"):
            logger.info(
                "retract_editor_text fenced by rebuild: %s (rid=%s)",
                failure_reason, request_id,
            )
            return
        logger.info(
            "retract_editor_text declined: %s (rid=%s)",
            failure_reason, request_id,
        )

    def _handle_insert_editor_word_response(self, message: dict) -> None:
        """Dispatch an inbound insert_editor_word_response (Section 5).

        Validates the payload through the shared schema (slice 18.1 /
        wh-g2-refactor.21.2) and resolves the pending future via the
        EditorPendingRequestMap. A late response (after the producer
        has timed out and popped) hits the unknown-id branch and is
        logged once.
        """
        from services.wheelhouse.shared.insert_editor_word import (
            InsertEditorWordResponse,
            InsertEditorWordSchemaError,
        )
        try:
            response = InsertEditorWordResponse.from_dict(message)
        except InsertEditorWordSchemaError as exc:
            logger.warning(
                "Dropping malformed insert_editor_word_response: %s", exc,
            )
            return
        payload = {
            "chars_inserted": response.chars_inserted,
            "clusters_inserted": response.clusters_inserted,
            "failure_reason": response.failure_reason,
        }
        if not self._insert_pending.complete(response.request_id, payload):
            logger.warning(
                "insert_editor_word_response for unknown rid=%s "
                "(timed out?)",
                response.request_id,
            )

    def _handle_retract_editor_text_response(self, message: dict) -> None:
        """Dispatch an inbound retract_editor_text_response (Section 2)."""
        from services.wheelhouse.shared.retract_editor_text import (
            RetractEditorTextResponse,
            RetractEditorTextSchemaError,
        )
        try:
            response = RetractEditorTextResponse.from_dict(message)
        except RetractEditorTextSchemaError as exc:
            logger.warning(
                "Dropping malformed retract_editor_text_response: %s", exc,
            )
            return
        payload = {
            "chars_requested": response.chars_requested,
            "chars_removed": response.chars_removed,
            "replay_chars": response.replay_chars,
            "failure_reason": response.failure_reason,
            "whole_utterance": response.whole_utterance,
        }
        if not self._retract_pending.complete(response.request_id, payload):
            logger.warning(
                "retract_editor_text_response for unknown rid=%s "
                "(timed out?)",
                response.request_id,
            )

    def _handle_editor_rebuilt(self, message: dict) -> None:
        """Dispatch an inbound editor_rebuilt notification (Section 6).

        Delegates to LogicRebuildFanout, which validates the payload,
        bumps observed_generation, and fans out failures to every
        stale pending future across both maps.
        """
        self._editor_rebuild_fanout.handle_notification(message)

    async def _update_zipformer_gpu_config(self, use_gpu: bool) -> None:
        """Update the Zipformer config.toml use_gpu setting.

        Args:
            use_gpu: True to enable GPU mode, False for CPU mode.
        """
        remote_launcher = getattr(self.service_manager, 'remote_stt_launcher', None)
        if not remote_launcher:
            logger.warning("Cannot update Zipformer config: no remote_stt_launcher")
            return

        provider_info = remote_launcher.get_provider_by_name("zipformer")
        if not provider_info:
            logger.warning("Cannot update Zipformer config: provider not found")
            return

        config_path = provider_info["service_dir"] / "config.toml"
        if not config_path.exists():
            logger.warning(f"Cannot update Zipformer config: {config_path} not found")
            return

        try:
            # Read existing config
            content = config_path.read_text(encoding="utf-8")

            # Replace use_gpu line (handles both true and false values)
            import re
            new_content = re.sub(
                r'^use_gpu\s*=\s*(true|false)\s*$',
                f'use_gpu = {"true" if use_gpu else "false"}',
                content,
                flags=re.MULTILINE
            )

            # Write back
            config_path.write_text(new_content, encoding="utf-8")
            logger.info(f"Updated Zipformer config: use_gpu = {use_gpu}")

        except Exception as e:
            logger.error(f"Failed to update Zipformer config: {e}")

    async def _handle_pattern_manager_action(self, action: str, command: dict):
        """Handle Pattern Manager IPC actions from the GUI."""
        from speech.pattern_manager import PatternManager

        try:
            # Everything that can raise -- resolving the speech handler,
            # constructing the PatternManager -- must run inside this try:
            # the GUI blocks on an f"{action}_result" envelope for every
            # request, so a pre-dispatch failure that escaped to the listener
            # loop's generic except would leave it waiting forever
            # (wh-pattern-editor-r0.2).
            speech_handler = self.service_manager.speech_handler
            pm = PatternManager(
                speech_handler.patterns_file, speech_handler.user_patterns_file,
            )
            data = command.get("data", {})

            def _reload_and_refresh() -> bool:
                # After a user-file write, reload both pattern files, push any
                # hotword change onto the running speech processor so a new wake
                # word takes effect without a restart (wh-user-patterns-split.4/.5),
                # and refresh the text parser's pattern list. reload() is
                # deliberately non-raising: on failure it keeps the old data
                # and returns False. On that path the running state (hotword,
                # parser patterns) is left untouched -- re-applying it from
                # the stale catalog would just reinstate the old data -- and
                # the caller adds a warning to its still-successful envelope
                # (wh-pattern-editor-r0.8).
                catalog = speech_handler.pattern_catalog
                if not catalog.reload():
                    return False
                if catalog.command_hotword:
                    speech_handler.apply_hotword(catalog.command_hotword)
                speech_handler.text_parser.patterns = catalog.get_all_patterns()
                return True

            def _stale_warning(verb: str) -> str:
                # The file write succeeded but the live patterns kept the old
                # data; success stays True and the GUI renders this banner
                # (wh-pattern-editor-r0.8).
                return (
                    f"{verb}, but the running patterns could not be "
                    "refreshed. Restart WheelHouse or try saving again."
                )

            if action == "pm_get_patterns":
                result = pm.get_all_patterns_structured()
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_patterns_data",
                    "data": result,
                })

            elif action == "pm_create_pattern":
                # wh-pattern-editor-dialog: the simple-mode editor sends a
                # phrases list INSTEAD of a trigger (the phrase list replaces
                # the trigger field), so both keys are optional here and
                # create_pattern resolves whichever is present.
                # wh-pattern-editor-advanced: the advanced editor instead
                # sends a raw 'expression' plus ordered raw 'actions' steps,
                # so action_type/action_params are optional too.
                result = pm.create_pattern(
                    trigger=data.get("trigger", ""),
                    pattern_type=data["pattern_type"],
                    action_type=data.get("action_type"),
                    action_params=data.get("action_params"),
                    requires_hotword=data.get("requires_hotword", False),
                    phrases=data.get("phrases"),
                    expression=data.get("expression"),
                    actions=data.get("actions"),
                    position=data.get("position"),
                )
                if result["success"] and not _reload_and_refresh():
                    result["warning"] = _stale_warning("Saved")
                # Echo the dialog's request_id so a save answer can be
                # matched to the save that asked for it
                # (wh-pattern-editor-r8.6).
                request_id = data.get("request_id")
                if request_id is not None:
                    result["request_id"] = request_id
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_create_result",
                    "data": result,
                })

            elif action == "pm_update_pattern":
                # Spec section 7 (wh-pattern-editor-update): payload carries
                # the target's id plus a create-shaped data dict. On success
                # the same live refresh as create/delete makes the edit take
                # effect without a restart.
                result = pm.update_pattern(
                    data["pattern_id"], data.get("data", {}),
                )
                if result["success"] and not _reload_and_refresh():
                    result["warning"] = _stale_warning("Saved")
                request_id = data.get("request_id")
                if request_id is not None:
                    result["request_id"] = request_id
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_update_result",
                    "data": result,
                })

            elif action == "pm_delete_pattern":
                result = pm.delete_pattern(data["pattern_id"])
                if result["success"] and not _reload_and_refresh():
                    result["warning"] = _stale_warning("Deleted")
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_delete_result",
                    "data": result,
                })

            elif action == "pm_validate_pattern":
                result = pm.validate_pattern(data["trigger"], data["pattern_type"])
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_validate_result",
                    "data": result,
                })

            elif action == "pm_set_hotword":
                result = pm.set_hotword(data["hotword"])
                if result["success"] and not _reload_and_refresh():
                    result["warning"] = _stale_warning("Saved")
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_set_hotword_result",
                    "data": result,
                })

            elif action == "pm_test_phrase":
                # wh-pattern-editor-test-messages (spec section 7): answer
                # the manager try-it box with the SAME in-memory objects the
                # runtime [PARSE] path matches with -- text_parser.patterns
                # plus its matcher, never a re-read of the files -- so the
                # answer cannot drift from live behavior.
                from speech.pattern_tester import run_test_phrase

                parser = speech_handler.text_parser
                result = run_test_phrase(
                    data["text"], parser.patterns, parser.matcher,
                )
                # Echo the sender's request_id so the dialog can pair the
                # answer with the request that produced it and drop an
                # out-of-date one (wh-pattern-editor-r6.1).
                request_id = data.get("request_id")
                if request_id is not None:
                    result["request_id"] = request_id
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_test_phrase_result",
                    "data": result,
                })

            elif action == "pm_test_draft":
                # Same in-memory objects; the module compiles the draft in
                # isolation (a bad draft is a draft_error in a SUCCESS
                # envelope, not a handler failure) and simulates the
                # catalog merge to answer which pattern responds first.
                from speech.pattern_tester import run_test_draft

                parser = speech_handler.text_parser
                result = run_test_draft(
                    data["draft"], data["text"],
                    parser.patterns, parser.matcher,
                )
                request_id = data.get("request_id")
                if request_id is not None:
                    result["request_id"] = request_id
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "pm_test_draft_result",
                    "data": result,
                })

            else:
                logger.warning(f"Unknown pattern manager action: {action}")

        except Exception as e:
            logger.error(f"Pattern manager action '{action}' failed: {e}", exc_info=True)
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": f"{action}_result" if not action.endswith("_result") else action,
                "data": {"success": False, "error": str(e)},
            })

    async def add_soft_allow(
        self,
        process_name: str,
        class_name: str,
        control_type: str,
    ) -> "AddSoftAllowOutcome":
        """Persist a soft-allow tuple, then push it to the input process.

        wh-9weum Phase 3 (wh-01t75). Disk write happens first via
        utils.soft_allow_writer.append_soft_allow_tuple. The IPC command
        only fires after a successful disk write so the in-memory state
        on the input process never diverges from disk in a way that
        survives a restart. On disk failure (False return or unexpected
        exception from the writer) the GUI state queue receives a
        soft_allow_write_failed event so Phase 4 can surface a
        'couldn't save' notice. An unexpected exception from the writer
        is caught here, surfaces the same notice the False-return path
        emits, and the method returns DISK_FAILED (wh-27gvv.2.1,
        deepseek review).

        wh-vbvgf.9.2 (codex review): the return type distinguishes the
        three outcomes so the Yes-path handler can apply the right
        post-grant policy:

          * SUCCESS    -- disk + IPC both ok. Counter should be reset.
          * IPC_FAILED -- disk wrote successfully, every IPC send
                           attempt raised (one initial send plus one
                           retry per _SOFT_ALLOW_IPC_RETRY_DELAYS
                           entry, wh-grant-ipc-failed-ux). The grant
                           is durable on disk and will load into the
                           input process on the next launcher run.
                           Counter should still be reset because the
                           soft-allow file owns the grant; the
                           in-memory input mirror will refresh on next
                           start.
          * DISK_FAILED -- disk write failed; IPC was not attempted.
                           The grant is not durable. Counter must NOT
                           be reset (per bead spec wh-8d81z) so the
                           user can click Yes again later.

        The method does not raise; callers branch on the outcome.
        """
        added_at = _utc_now_iso()
        path = self._resolve_soft_allow_path()

        try:
            wrote = await asyncio.to_thread(
                append_soft_allow_tuple,
                (process_name, class_name, control_type, added_at),
                path,
            )
        except Exception as exc:
            # wh-27gvv.2.1 (deepseek review): the writer catches OSError
            # and returns False, but a future writer change, a path
            # override mistake, or a serialisation failure could raise
            # something else. Without this catch the failure feedback
            # never reaches the GUI: the handler's outer wrapper logs
            # but does not enqueue soft_allow_write_failed, so the user
            # gets neither the durable Yes behaviour nor the
            # "couldn't save" notice. Catching here keeps the user-
            # visible recovery path consistent across all failure
            # modes (mirror of the wh-27gvv.1.1 fix in add_declined).
            logger.warning(
                "add_soft_allow: writer raised for %s: %s -- "
                "treating as disk failure",
                path, exc,
                exc_info=True,
            )
            wrote = False

        if not wrote:
            logger.warning(
                "add_soft_allow: disk write failed for %s -- IPC not sent",
                path,
            )
            try:
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "soft_allow_write_failed",
                    "process_name": process_name,
                    "class_name": class_name,
                    "control_type": control_type,
                })
            except Exception as exc:
                logger.error(
                    "add_soft_allow: could not enqueue failure event: %s",
                    exc,
                )
            return AddSoftAllowOutcome.DISK_FAILED

        # wh-grant-ipc-failed-ux (deepseek wh-soft-allow-verdict-tier.2.2):
        # retry a failed IPC send before reporting IPC_FAILED. Without the
        # retry, a transient queue hiccup left the running input process
        # without the grant, so the user kept seeing the same rejection
        # notice they had just clicked Yes on until the next restart.
        # Replays are safe: the input-side handler adds to a set.
        payload = {
            "process_name": process_name,
            "class_name": class_name,
            "control_type": control_type,
        }
        delays = getattr(self, "_soft_allow_ipc_retry_delays", None)
        if not isinstance(delays, (tuple, list)):
            delays = _SOFT_ALLOW_IPC_RETRY_DELAYS
        attempts = 1 + len(delays)
        last_exc: Optional[Exception] = None
        for attempt, delay in enumerate((0.0, *delays), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self.app.send_command("add_soft_allow_tuple", payload)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "add_soft_allow: IPC send attempt %d/%d failed after "
                    "successful disk write: %s",
                    attempt, attempts, exc,
                )
                continue
            if attempt > 1:
                logger.info(
                    "add_soft_allow: IPC send succeeded on attempt %d/%d",
                    attempt, attempts,
                )
            return AddSoftAllowOutcome.SUCCESS
        logger.error(
            "add_soft_allow: IPC send failed on all %d attempts after "
            "successful disk write -- grant is durable on disk and will "
            "apply on next start: %s",
            attempts, last_exc,
            exc_info=last_exc,
        )
        return AddSoftAllowOutcome.IPC_FAILED

    def _resolve_soft_allow_path(self) -> "Path":
        """Return the path to the soft-allow file.

        Tests can override the location by setting
        ``self._soft_allow_path`` on the controller before invoking
        ``add_soft_allow``. Production resolves through
        utils.system.get_user_data_dir (wh-k8ef), the same helper the
        TextTargetPredicate loader uses, so the file the input process
        reads at startup matches the file the logic process writes --
        in both the source checkout and a frozen build.
        """
        from utils.system import get_user_data_dir

        override = getattr(self, "_soft_allow_path", None)
        if override is not None:
            return override
        return get_user_data_dir() / "soft_allow_tuples.toml"

    def _resolve_declined_path(self) -> "Path":
        """Return the path to the declined-tuple file (wh-27gvv).

        Tests can override the location by setting
        ``self._declined_path`` on the controller before invoking
        ``add_declined`` or ``_load_declined_tuples``. Production
        resolves through utils.system.get_user_data_dir (wh-k8ef), the
        same helper the writer path uses.
        """
        from utils.system import get_user_data_dir

        override = getattr(self, "_declined_path", None)
        if override is not None:
            return override
        return get_user_data_dir() / "soft_allow_declined_tuples.toml"

    def _resolve_pending_counters_path(self) -> "Path":
        """Return the path to the pending-counters file (wh-82lnx).

        Resolves through utils.system.get_user_data_dir (wh-k8ef) so
        the retry counters stay alongside the other soft-allow user
        state in both the source checkout and a frozen build.
        """
        from utils.system import get_user_data_dir

        return get_user_data_dir() / "soft_allow_pending_counters.toml"

    def _load_declined_tuples(self) -> None:
        """Seed _grant_prompt_no_suppressed from disk at startup (wh-27gvv).

        The Logic process consults the suppression set in
        _on_retry_threshold_reached before forwarding the three-strikes
        approval prompt to the GUI. Loading at startup is what makes
        the No choice survive a restart.

        Read errors and malformed entries are handled by
        parse_declined_file: missing file is the documented initial
        state, malformed file is treated as empty with a WARNING log,
        per-entry validation failures are logged at WARNING when
        log_skipped_entries=True so a hand-edit mistake surfaces.
        """
        from shared.declined_tuples_schema import parse_declined_file

        path = self._resolve_declined_path()
        entries = parse_declined_file(
            path,
            log_skipped_entries=True,
            caller="declined loader",
        )
        for entry in entries:
            self._grant_prompt_no_suppressed.add(
                (entry.process_name, entry.class_name, entry.control_type),
            )

    async def add_declined(
        self,
        process_name: str,
        class_name: str,
        control_type: str,
    ) -> bool:
        """Persist a declined tuple, then update the in-memory set (wh-27gvv).

        Writes the new entry through ``append_declined_tuple`` (atomic
        temp + fsync + os.replace). On a successful write the
        in-memory ``_grant_prompt_no_suppressed`` set is updated and
        the method returns True. On a failed write the in-memory set
        is left untouched, a ``declined_write_failed`` action is
        enqueued on the GUI state queue, and the method returns
        False. The method does not raise; an unexpected exception
        from the writer is caught here, surfaces the same
        ``declined_write_failed`` notice the False-return path emits,
        and returns False (wh-27gvv.1.1, codex review).

        Disk-first ordering matches add_soft_allow: the disk and the
        running process either agree, or a restart recovers any
        decline that landed on disk before the in-memory update was
        attempted.
        """
        added_at = _utc_now_iso()
        path = self._resolve_declined_path()

        try:
            wrote = await asyncio.to_thread(
                append_declined_tuple,
                (process_name, class_name, control_type, added_at),
                path,
            )
        except Exception as exc:
            # wh-27gvv.1.1 (codex review): the writer catches OSError
            # and returns False, but a future writer change, a path
            # override mistake, or a serialisation failure could raise
            # something else. Without this catch the failure feedback
            # never reaches the GUI: the handler's outer wrapper logs
            # but does not enqueue declined_write_failed, so the user
            # gets neither the persisted suppression nor the
            # "couldn't save" notice. Catching here keeps the user-
            # visible recovery path consistent across all failure
            # modes.
            logger.warning(
                "add_declined: writer raised for %s: %s -- "
                "in-memory set not updated; treating as disk failure",
                path, exc,
                exc_info=True,
            )
            wrote = False

        if not wrote:
            logger.warning(
                "add_declined: disk write failed for %s -- "
                "in-memory set not updated",
                path,
            )
            try:
                self.state_manager.state_to_gui_queue.put_nowait({
                    "action": "declined_write_failed",
                    "process_name": process_name,
                    "class_name": class_name,
                    "control_type": control_type,
                })
            except Exception as exc:
                logger.error(
                    "add_declined: could not enqueue failure event: %s",
                    exc,
                )
            return False

        self._grant_prompt_no_suppressed.add(
            (process_name, class_name, control_type),
        )
        return True

    def _build_gui_handler_map(self, command: dict) -> dict:
        """Build the action -> callable map for the GUI command listener.

        Extracted from the listener loop body so tests can verify each
        action key is bound to the right handler method (wh-vbvgf.13.3
        deepseek finding). The source-inspection regression test does
        not catch a copy-paste error that binds the wrong handler to
        the right key; calling this method directly and asserting the
        binding does.

        Lambdas close over the ``command`` dict so per-call payload
        values are visible to the handler. The map is rebuilt for
        every command, which costs a small dict allocation but keeps
        the closure semantics simple.
        """
        return {
            "request_initial_state": self.state_manager.send_state_update,
            "toggle_speech_enabled_state": self.state_manager.toggle_speech_enabled_state,
            "ptt_start": lambda: self.state_manager.ptt_start(source=command.get("source", "unknown")),
            "ptt_stop": lambda: self.state_manager.ptt_stop(reason=command.get("reason", "released")),
            "set_speech_interaction_mode": lambda: self.state_manager.set_speech_interaction_mode(command.get("mode", "toggle")),
            "toggle_button_visibility": self.state_manager.toggle_button_visibility,
            "toggle_log_level": self.toggle_log_level,
            "toggle_interim_results": self.toggle_interim_results,
            "restart_program": self.restart_program,
            "restart_stt_service": self.restart_stt_service,
            "hard_restart_stt_service": self.hard_restart_stt_service,
            "set_config_value": lambda: self.create_task_with_error_handling(self.state_manager.set_config_value(command.get('key'), command.get('value')), "SetConfigValue"),
            "switch_stt_provider": lambda: self.create_task_with_error_handling(self._switch_stt_provider(command.get('provider')), "SwitchSTTProvider"),
            "switch_ai_provider": lambda: self.create_task_with_error_handling(self._switch_ai_provider(command.get('provider')), "SwitchAIProvider"),
            "help_ask": lambda: self.create_task_with_error_handling(self._handle_help_ask(command.get("question", "")), "HelpAsk"),
            "help_reset": lambda: self._handle_help_reset(),
            "help_cancel": lambda: self._handle_help_cancel(),
            "te_cancelled": lambda: self.create_task_with_error_handling(
                self._handle_te_cancelled(), "TECancelled",
            ),
            "te_event_ack": lambda: self.create_task_with_error_handling(
                self._handle_te_event_ack(
                    command.get("request_id", ""),
                    command.get("op", ""),
                    command.get("editor_hwnd", 0),
                ),
                "TEEventAck",
            ),
            # wh-iycks: GUI emits this when the user clicks
            # "Try it anyway" on a rejection toast. The handler
            # resolves the correlation_token against the
            # Logic-side token cache and either fires the retry
            # pipeline or surfaces a click-too-late toast.
            "try_anyway_clicked": lambda: self.create_task_with_error_handling(
                self._handle_try_anyway_clicked(command),
                "TryAnywayClicked",
            ),
            # wh-8d81z: GUI emits this when the user clicks Yes
            # on the three-strikes follow-up toast. The handler
            # writes the soft-allow file, sends add_soft_allow_tuple
            # IPC to the input process on disk-write success,
            # and resets the click counter for the tuple.
            "grant_prompt_yes_clicked": lambda: self.create_task_with_error_handling(
                self._handle_grant_prompt_yes_clicked(command),
                "GrantPromptYesClicked",
            ),
            # wh-vdt1t: GUI emits this when the user clicks No
            # on the three-strikes follow-up toast. The handler
            # records the tuple in _grant_prompt_no_suppressed
            # so subsequent RetryThresholdReached events for
            # that tuple drop their GUI forward.
            "grant_prompt_no_clicked": lambda: self.create_task_with_error_handling(
                self._handle_grant_prompt_no_clicked(command),
                "GrantPromptNoClicked",
            ),
            # wh-jfavj / wh-g4oma: GUI emits this when the user clicks a
            # numbered overlay item (Phase 1.5 voice element-clicking). The
            # handler resolves the display_number against the retained
            # snapshot cache and, on SNAPSHOT_EXPIRED or NOT_FOUND, forwards
            # the shipped snapshot_expired click notice. FOUND defers to the
            # not-yet-wired overlay-click execution slice.
            "snapshot_item_clicked": lambda: self.create_task_with_error_handling(
                self._handle_snapshot_item_clicked(command),
                "SnapshotItemClicked",
            ),
            # wh-n29v.67: GUI emits this after it applies / fails / clears a
            # numbered-overlay paint. The handler validates the payload, gates on
            # the overlay being active, maps the wire state to a PAINT_ACK
            # OverlayEvent carrying the wire generation pair, and applies it
            # through _apply_overlay_event so the held overlay state machine is
            # driven (the machine's own generation gate drops a stale ack).
            "overlay_state_changed": lambda: self.create_task_with_error_handling(
                self._handle_overlay_state_changed(command),
                "OverlayStateChanged",
            ),
            # wh-g2-refactor.18 (Section 5): per-word editor insert
            # responses. The handler validates the payload through the
            # shared schema and resolves the matching pending future.
            "insert_editor_word_response": lambda: (
                self._handle_insert_editor_word_response(command)
            ),
            # wh-g2-refactor.18 (Section 2): retract+replay responses.
            "retract_editor_text_response": lambda: (
                self._handle_retract_editor_text_response(command)
            ),
            # wh-g2-refactor.18 (Section 6): rebuild notifications fan
            # out failures to every stale pending future in bulk so the
            # producer side does not wait for individual per-request
            # timeouts.
            "editor_rebuilt": lambda: self._handle_editor_rebuilt(command),
        }

    async def _listen_for_gui_commands(self, commands_from_gui_queue: Queue):
        """:flow: GUI State Synchronization
        :step: 3
        :description: Long-running task polling IPC queue for GUI commands
        :data_in: Command dictionaries from commands_from_gui_queue
        :data_out: Dispatched calls to handler functions
        :notes: Runs in logic process asyncio event loop. Uses asyncio.to_thread() to wrap blocking Queue.get() calls, preventing event loop blocking. The get() uses a bounded timeout (wh-logic-exit-hang): the GUI process is the queue's only producer, so an unbounded get() parks the executor worker thread forever once the GUI exits during shutdown, and asyncio.run() teardown then hangs joining the default executor until the launcher hard-terminates the process. Processes commands from GUI process (step 2) and routes them to handlers (step 4). This is the inbound half of bidirectional GUI↔Logic IPC. Runs until shutdown_event is set.
        """
        logger.info("GUI command listener started.")
        # Bounded so the worker thread re-checks shutdown at least once
        # per interval; must stay well under the launcher's 5s
        # shutdown grace period (wh-logic-exit-hang).
        poll_timeout_s = 1.0
        while not self.shutdown_event.is_set():
            try:
                command = await asyncio.to_thread(
                    commands_from_gui_queue.get, True, poll_timeout_s
                )
                if self.shutdown_event.is_set():
                    break
                
                action = command.get("action")

                # Pattern Manager actions (need access to full command dict)
                if action and action.startswith("pm_"):
                    await self._handle_pattern_manager_action(action, command)
                    continue

                """:flow: GUI State Synchronization
                :step: 4
                :description: Routes command action to appropriate handler function
                :data_in: Action string from command dictionary
                :data_out: Invocation of mapped handler function
                :notes: Command dispatcher using handler_map dictionary. Supported actions: request_initial_state, toggle_speech_enabled_state, toggle_button_visibility, toggle_log_level, restart_program, restart_stt_service, set_config_value. Handles both sync and async handlers - uses asyncio.iscoroutinefunction() to detect async handlers and wraps them with create_task_with_error_handling(). Unknown actions logged as warnings but don't crash the listener.
                """
                handler_map = self._build_gui_handler_map(command)

                handler = handler_map.get(action)
                if handler:
                    if asyncio.iscoroutinefunction(handler):
                        self.create_task_with_error_handling(handler(), f"GUICmd_{action}")
                    else:
                        handler()
                else:
                    logger.warning(f"Received unknown action from GUI: {action}")

            except queue_module.Empty:
                # Idle poll interval elapsed; loop around and re-check
                # the shutdown event (wh-logic-exit-hang).
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in GUI command listener: {e}", exc_info=True)
        logger.info("GUI command listener shutting down.")

    async def shutdown(self):
        """:flow: Application Lifecycle
        :step: 6
        :produces_for: Application Lifecycle
        :description: Orchestrates graceful shutdown of the logic process
        :data_in: Shutdown request
        :data_out: Clean process termination
        :notes: The central shutdown coordinator. (1) Cancels pending state saves. (2) Calls ServiceManager.shutdown_services() (Step 7). (3) Cancels all LogicController background tasks. (4) Calls App.shutdown() (Step 8). Ensures no resources are leaked and the process exits cleanly.
        """
        """
        Performs a graceful shutdown of the logic process.

        This involves cancelling all background tasks, shutting down services,
        and cleaning up application resources.
        """
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self.shutdown_requested = True
        logger.info("Initiating shutdown of logic process.")

        # wh-c169t: clear the opt-in Windows screen-reader flag FIRST, before
        # the (potentially slow or hangable) service/network teardown below, so
        # PSReadLine recovers in subsequent PowerShell sessions even if a later
        # teardown step stalls. The launcher force-terminates each process after
        # SHUTDOWN_GRACE_PERIOD_S (5s); a hung shutdown_services() would defeat
        # a clear placed at the end (wh-9f3t.36.2). The clear has no dependency
        # on services, so running it first is safe. The early-return guard above
        # keeps this from running twice; clear_screen_reader_flag never raises,
        # so it cannot block clean shutdown; getattr-with-default guards the case
        # where __init__ was bypassed or startup aborted before the attribute
        # was set, so shutdown never raises AttributeError here.
        if getattr(self, "_screen_reader_flag_enabled", False):
            clear_screen_reader_flag()
            logger.info("Screen-reader flag CLEARED on graceful shutdown.")

        # wh-n29v.21: stop the overlay focus-hook thread and unhook both the
        # foreground hook and any transient destroy hook so no Win32 hook is
        # leaked across shutdown. getattr guard for the bypassed-__init__ case;
        # _stop_overlay_focus_hooks is itself a no-op when no manager exists.
        if getattr(self, "_overlay_focus_hooks", None) is not None:
            self._stop_overlay_focus_hooks()

        # wh-n29v.96.2 / wh-n29v.95: cancel the per-state timeout timer, the
        # 200ms hold timer, and the periodic keepalive timer so no overlay timer
        # outlives shutdown. getattr guards for the bypassed-__init__ case.
        for _timer_attr in (
            "_overlay_timer", "_overlay_hold_timer", "_overlay_keepalive_timer",
        ):
            _t = getattr(self, _timer_attr, None)
            if _t is not None:
                try:
                    _t.cancel()
                except Exception:  # noqa: BLE001 -- never block shutdown
                    pass
                setattr(self, _timer_attr, None)

        # wh-tab7j: drop every retained WalkSnapshotSummary (eviction
        # trigger 3). getattr guard for the bypassed-__init__ case.
        cache = getattr(self, "click_snapshot_summary_cache", None)
        if cache is not None:
            try:
                cache.clear()
            except Exception:  # noqa: BLE001 -- never block shutdown
                pass

        await self.state_manager.cancel_pending_saves()
        await self.service_manager.shutdown_services()

        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)

        await self.app.shutdown()

        logger.info("Logic process shutdown complete.")

    def _apply_startup_screen_reader_flag(self) -> None:
        """Read [click] config and apply the screen-reader flag at startup.

        Extracted from main() so the assign-and-apply wiring (config -> intent
        -> self._screen_reader_flag_enabled -> apply_screen_reader_flag) is
        unit-testable without running the full async main()
        (wh-69sk8 / wh-9f3t.40.1). Best-effort: never raises.
        """
        from services.wheelhouse.ui.click_config import ClickConfig

        click_cfg = ClickConfig.from_raw(self.config_service.get("click", {}))
        self._screen_reader_flag_enabled = _screen_reader_flag_intent(click_cfg)
        apply_screen_reader_flag(self._screen_reader_flag_enabled)
        if self._screen_reader_flag_enabled:
            logger.info(
                "Screen-reader flag SET at startup (voice-clicking opt-in is "
                "on; PSReadLine will be disabled in PowerShell sessions until "
                "shutdown)."
            )
        else:
            logger.info(
                "Screen-reader flag NOT enabled at startup (opt-in off or "
                "voice-clicking disabled); any stale WheelHouse-owned flag "
                "is cleared only if the ownership marker is present."
            )

    async def main(self, commands_from_gui_queue: Queue):
        """:flow: Application Lifecycle
        :step: 2
        :consumes_from: Application Lifecycle
        :produces_for: Application Lifecycle
        :description: Main logic loop: initializes app, services, and monitors tasks
        :data_in: IPC queues from initialization
        :data_out: Running application state
        :notes: The core execution loop. (1) Starts IPC via App.start() (Step 3). (2) Initializes services via ServiceManager (Step 4). (3) Starts services (Step 5). (4) Launches GUI command listener and state updater. (5) Enters main wait loop watching for shutdown events. Catches critical exceptions and triggers shutdown.
        """
        """
        The main entry point for the application's logic.

        This method initializes and starts all services and background tasks,
        sets up signal handling, and waits for a shutdown signal.

        Args:
            commands_from_gui_queue (Queue): The queue for receiving commands from the GUI process.
        """
        from integrations.speech_to_text_server import start_websocket_server

        try:
            logger.info(f"Main application logic running in PID: {os.getpid()}")
            self.loop.set_exception_handler(self.async_exception_handler)
            signal.signal(signal.SIGINT, self.handle_exit_signal)
            signal.signal(signal.SIGTERM, self.handle_exit_signal)

            # wh-c169t: apply the opt-in Windows screen-reader flag. Read the
            # validated config via ClickConfig.from_raw (the only never-raising
            # reader; do not read the raw key directly). The flag is SET only
            # when the voice-clicking feature is enabled AND the user opted in
            # (enable_screen_reader_flag). Gating on `enabled` too matters: the
            # valid combination enabled=false + enable_screen_reader_flag=true
            # survives validation, and setting a machine-wide flag that breaks
            # PSReadLine for a feature that is globally OFF is wrong
            # (wh-9f3t.36.1). In every other case the flag is CLEARED
            # unconditionally as self-recovery from a crashed session that may
            # have left it set. Best-effort: it never aborts startup.
            self._apply_startup_screen_reader_flag()

            # wh-n29v.21: start the Logic-side overlay focus hooks (foreground
            # SetWinEventHook + transient destroy hook). The method gates on
            # click_config.enabled AND overlay_enabled_effective (wh-n29v.66.1.1),
            # so a bad overlay key that leaves enabled True but disables the
            # overlay does not start the hooks; best-effort, never aborts startup.
            self._start_overlay_focus_hooks()

            # Determine if we should start WebSocket to remote STT server
            stt_mode = self.config_service.get("stt.mode", "remote")
            start_websocket = (stt_mode == "remote")

            # Start the app's internal components (IPC tasks, optionally WebSocket)
            # WebSocket skipped when stt.mode = "in_process" to prevent dual STT sources
            await self.app.start(
                self.config_service.get("SPEECH_WEBSOCKET_HOST", "localhost"),
                0,  # OS assigns free port; actual port read back from server socket
                self.handle_transcribed_text,
                start_websocket=start_websocket,
            )
            
            # Register handler for unsolicited events from Input Process
            self.app.register_event_handler(self._handle_input_event)

            # Now that the app has started, the websocket_manager is available.
            # We can now assign it to the state_manager and update with speech_handler
            self.state_manager.websocket_manager = self.app.websocket_manager
            # Set reverse reference for notifications
            self.app.websocket_manager.state_manager = self.state_manager
            # Wire up app reference for utterance lifecycle UI commands
            self.app.websocket_manager.set_app(self.app)
            
            # Connect to GUI shared memory for activity state updates
            if self.gui_shm_name:
                self.app.websocket_manager.set_gui_shm(self.gui_shm_name)

            self.service_manager.initialize_services()

            # Update RemoteSTTLauncher with the actual port from the WebSocket server
            if start_websocket and self.service_manager.remote_stt_launcher:
                self.service_manager.remote_stt_launcher.ws_port = self.app.ws_port
                logger.info(f"Remote STT launcher updated with actual WebSocket port: {self.app.ws_port}")

            # Initialize and start the new speech processor (Phase 5.2)
            # This must happen AFTER services are initialized so speech_handler exists
            if self.service_manager.speech_handler and self.app.websocket_manager:
                self.app.websocket_manager.speech_handler = self.service_manager.speech_handler
                logger.info("Initializing SpeechProcessor with word_queue from WebSocketManager")
                self.service_manager.speech_handler.initialize_speech_processor(
                    self.app.websocket_manager.word_queue
                )
                await self.service_manager.speech_handler.speech_processor.start()
                logger.info("SpeechProcessor started successfully")
            
            # Send the initial state to the GUI to ensure it initializes correctly.
            self.state_manager.send_state_update()
            
            # Start STT based on mode
            if stt_mode == "in_process" and self.service_manager.stt_manager:
                await self.service_manager.start_stt_manager(self._handle_stt_transcript)
                logger.info("In-process STT started")
            elif stt_mode == "remote" and self.service_manager.remote_stt_launcher:
                # Wire up WebSocket manager for shutdown commands
                self.service_manager.remote_stt_launcher.set_websocket_manager(
                    self.app.websocket_manager
                )
                # Wire up notification callback for loading/ready messages
                self.service_manager.remote_stt_launcher.set_notify_callback(
                    self.state_manager.speech_notifier._send_notification
                )
                # Wire up working dialog callbacks for loading/ready messages
                gui_queue = self.state_manager.state_to_gui_queue

                def _show_working_via_queue(message: str):
                    try:
                        gui_queue.put_nowait({"action": "show_working", "message": message})
                    except Exception:
                        pass

                def _hide_working_via_queue():
                    try:
                        gui_queue.put_nowait({"action": "hide_working"})
                    except Exception:
                        pass

                self.service_manager.remote_stt_launcher.set_working_callback(
                    show=_show_working_via_queue,
                    hide=_hide_working_via_queue,
                )
                # Wire up reverse reference so WebSocket manager can signal provider ready
                self.app.websocket_manager.remote_stt_launcher = self.service_manager.remote_stt_launcher
                # Auto-start the last selected remote STT provider
                self.service_manager.start_remote_stt()
                logger.info("Remote STT provider auto-start initiated")
            
            # Consolidate all essential background tasks
            service_tasks = self.service_manager.start_services()
            
            # The GUI command listener is a critical background task.
            # We create it here and add it to our list of tasks to monitor.
            gui_listener_task = self.create_task_with_error_handling(
                self._listen_for_gui_commands(commands_from_gui_queue), 
                "GuiCommandListener"
            )

            task_coroutines = {
                "PeriodicStateUpdate": self.periodic_state_updater()
            }

            for name, coro in task_coroutines.items():
                self.create_task_with_error_handling(coro, name)

            logger.info(f"LogicController setup complete with {len(self.background_tasks)} tasks. Running...")
            
            asyncio_shutdown_event = asyncio.Event()
            self.create_task_with_error_handling(self.watch_shutdown_event(asyncio_shutdown_event), "ShutdownWatcher")
            await asyncio_shutdown_event.wait()
            
        except Exception as e:
            logger.critical(f"Critical unhandled exception in main: {e}", exc_info=True)
            self.request_shutdown()
        finally:
            await self.shutdown()

    async def periodic_state_updater(self):
        """
        A background task that periodically sends the complete application state to the GUI.
        This ensures the GUI remains synchronized even if some state change events are missed.
        The periodic_state_updater is the safety net that makes the system robust against these
        kinds of real-world, transient problems. It ensures that no matter what happens, 
        the GUI will always be brought back to the correct state within a few seconds.
        """
        while not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(3)
                if not self.shutdown_event.is_set():
                    self.state_manager.send_state_update()
            except asyncio.CancelledError:
                break

    async def watch_shutdown_event(self, asyncio_event: asyncio.Event):
        """
        A background task that bridges the multiprocessing shutdown Event to an asyncio Event.

        This allows the main asyncio loop to `await` a shutdown signal that
        can be triggered from a different process or a synchronous signal handler.

        Args:
            asyncio_event (asyncio.Event): The event to set when a shutdown is detected.
        """
        while not self.shutdown_event.is_set():
            await asyncio.sleep(0.2)
        logger.info("External shutdown event detected by watcher.")
        asyncio_event.set()

    def create_task_with_error_handling(self, coro, task_name: str):
        """
        Creates an asyncio task with a completion callback for error handling.

        This ensures that any exception in the task is logged and triggers a shutdown,
        preventing silent failures.

        Args:
            coro: The coroutine to run as a task.
            task_name (str): A descriptive name for the task for logging purposes.

        Returns:
            asyncio.Task: The created task object.
        """
        logger.debug(f"Creating task: {task_name}")
        task = self.loop.create_task(coro, name=task_name)
        self.background_tasks.append(task)
        task.add_done_callback(self._handle_task_completion)
        return task

    def _handle_task_completion(self, task: asyncio.Task):
        """
        Callback executed when a task created by `create_task_with_error_handling` finishes.

        It checks for exceptions and logs them, triggering a shutdown if an
        unexpected error occurred.

        Args:
            task (asyncio.Task): The completed task.
        """
        # Discard the finished task so self.background_tasks does not grow
        # without bound (wh-n29v.69.2). High-frequency fire-and-forget handlers
        # -- overlay_state_changed is emitted on every overlay paint / failed
        # paint / clear -- would otherwise retain one completed task object per
        # event for the lifetime of the Logic process, and shutdown would gather
        # over an ever-growing list of already-finished tasks. Safe to remove
        # here: background_tasks is read only at shutdown, whose cancel loop has
        # no await (no done-callback fires mid-iteration) and whose
        # asyncio.gather unpacks the list before awaiting.
        try:
            self.background_tasks.remove(task)
        except ValueError:
            pass
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug(f"Task '{task.get_name()}' was cancelled.")
        except Exception as e:
            logger.error(f"Task '{task.get_name()}' failed with an exception.", exc_info=e)
            self.request_shutdown()

    def _check_for_restart(self):
        """Checks if a restart has been requested."""
        try:
            # This path needs to be determined correctly, maybe from config or a shared location
            flag_path = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "WheelHouse", "wheelhouse.restart")
            if os.path.exists(flag_path):
                os.remove(flag_path)
                return True
        except Exception as e:
            logger.error(f"Error checking for restart flag: {e}")
        return False


def start_logic_process(shm_name: str, command_ready_event: Event, input_ready_event: Event, response_queue: "Queue[str]", shm_bytes: int, shutdown_event: Event, commands_from_gui_queue: "Queue[str]", state_to_gui_queue: "Queue[str]", gui_shm_name: str = None):
    """
    :flow: Application Lifecycle
    :step: 1
    :produces_for: Application Lifecycle
    :description: Entry point for Logic Process - initializes asyncio event loop, speech handlers, and all coordinators.
    :data_in: IPC primitives from main process (shared memory name, multiprocessing Events/Queues, shutdown signal).
    :data_out: Spawns asyncio loop running LogicController with all speech/action handlers initialized.
    :notes: Target function for multiprocessing.Process creation. Runs in separate process with own Python
        interpreter. Sets up process-local logging, creates WheelHouseApp (IPC layer), EventBus (pub/sub),
        StateManager (GUI state sync), ServiceManager (speech/audio services). Blocks on asyncio.run() until
        shutdown_event signals termination. This is the heart of WheelHouse - coordinates all speech processing,
        pattern matching, action execution flows.
        
    The target function for the logic multiprocessing.Process.

    This function sets up the entire environment for the logic process,
    including configuration, logging, and the main asyncio event loop. It
    initializes and runs the `LogicController`.

    Args:
        shm_name (str): Name of the shared memory block.
        command_ready_event (Event): Event to signal that the command queue is ready.
        input_ready_event (Event): Event to signal that the input process is ready.
        response_queue (Queue[str]): Queue for sending responses to the input process.
        shm_bytes (int): Size of the shared memory block.
        shutdown_event (Event): The master shutdown event.
        commands_from_gui_queue (Queue[str]): Queue for receiving commands from the GUI.
        state_to_gui_queue (Queue[str]): Queue for sending state updates to the GUI.
        gui_shm_name (str, optional): Name of GUI activity shared memory segment.
    """
    import sys
    
    config_service = ConfigService()
    setup_logging(config_service)
    
    # Display startup banner with version info
    logger.info(f"{get_startup_banner('WheelHouse')} - Starting")

    try:
        async def async_main():
            """Sets up and runs the main asynchronous application components."""
            loop = asyncio.get_running_loop()
            
            # Create managers and core components in the correct order
            app = WheelHouseApp(shm_name, command_ready_event, input_ready_event, response_queue, shm_bytes)
            event_bus = EventBus()
            
            # The app now creates the websocket_manager, so we pass None here
            # and retrieve it after app.start() is called.
            state_manager = StateManager(
                config_service=config_service, 
                event_bus=event_bus, 
                loop=loop, 
                state_to_gui_queue=state_to_gui_queue, 
                websocket_manager=None 
            )
            
            service_manager = ServiceManager(
                app=app, 
                config_service=config_service, 
                event_bus=event_bus, 
                loop=loop, 
                state_manager=state_manager
            )
            
            # The LogicController orchestrates everything
            controller = LogicController(
                app=app, 
                config_service=config_service, 
                shutdown_event=shutdown_event, 
                event_bus=event_bus, 
                service_manager=service_manager, 
                state_manager=state_manager,
                gui_shm_name=gui_shm_name
            )
            
            # Pass the controller to the service manager to resolve circular dependency
            service_manager.set_logic_controller(controller)
            
            await controller.main(commands_from_gui_queue)

        asyncio.run(async_main())
    except Exception as e:
        logger.critical("A critical, unhandled exception occurred during the async setup phase.", exc_info=True)
    except KeyboardInterrupt:
        logger.info("Logic process interrupted.")
    finally:
        logger.info("Logic process finished.")
        sys.exit(0)