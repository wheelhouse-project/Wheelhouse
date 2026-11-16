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

# Wheelhouse: put the owned Microsoft Visual C++ runtime folder on this
# process's library search path BEFORE any extension module loads. The order
# is the whole fix -- os.add_dll_directory cannot displace a library the
# process already holds. services/runtime_dll_directory.py explains it.
import os.path
import sys

_services_dir = os.path.abspath(__file__)
while (os.path.basename(_services_dir) != "services"
       and os.path.dirname(_services_dir) != _services_dir):
    _services_dir = os.path.dirname(_services_dir)
if _services_dir not in sys.path:
    sys.path.append(_services_dir)
from runtime_dll_directory import add_runtime_dll_directory

add_runtime_dll_directory()


import asyncio
import enum
import functools
import logging
import os
import queue as queue_module
import signal
import time
from multiprocessing import Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from collections.abc import Callable


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

# wh-overlay-slow-uia-stale-badges.14.19: backoff between terminal-editor
# control-command send retries after a queue-full drop. One initial send
# plus one retry per entry. Tests override per-instance via
# _te_control_send_retry_delays.
_TE_CONTROL_SEND_RETRY_DELAYS = (0.2, 0.5)

from services.wheelhouse.app import WheelHouseApp
from services.wheelhouse.click_counter import ClickCounter
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.numbered_overlay.controller import NumberedOverlayController
from services.wheelhouse.mouse_grid.controller import MouseGridController
from services.wheelhouse.events import RetryThresholdReached, RetryVerified
from services.wheelhouse.service_manager import ServiceManager
from services.wheelhouse.shared.consumed_token_set import ConsumedTokenSet
from services.wheelhouse.shared.dialog_owner import launch_owner_token
from services.wheelhouse.shared.rejection_token_cache import (
    RejectionTokenCache,
    RejectionTuple,
)
from services.wheelhouse.state_manager import StateManager
from services.wheelhouse.integrations.websocket_manager import WebSocketManager
from services.wheelhouse.utils.audio_suppression_decision import (
    AUDIO_SUPPRESSION_DEFAULT,
    AUDIO_SUPPRESSION_SETTING,
    decide_audio_suppression,
    parse_audio_suppression_setting,
)
from services.wheelhouse.utils.logging_setup import setup_logging
from services.wheelhouse.utils.screen_reader_flag import (
    apply_screen_reader_flag,
    clear_screen_reader_flag,
)
from services.wheelhouse.utils.soft_allow_writer import append_soft_allow_tuple
from services.wheelhouse.utils.declined_writer import append_declined_tuple
from services.wheelhouse.utils.system import get_app_data_path
from services.wheelhouse.config_service import (
    ConfigService,
    DEFAULT_STT_PROVIDER,
    warn_if_stt_mode_unsupported,
)

# Import version info from parent services directory
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))
from version_info import get_startup_banner
from stt import parakeet_model
import update_check
import update_install


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


# How long the code waits for a remote provider to exit before it
# decides that provider is still running. Two callers ask that question:
# _switch_stt_provider, when a replacement failed to start, and
# _reconcile_stopped_remote_provider, when a replacement that had already
# spawned failed later. Module-level so tests can shorten it
# (wh-remote-stt-robustness.2.2, .2.5).
_PROVIDER_EXIT_WAIT_S = 10.0


def _working_dialog_callbacks(gui_queue):
    """The launcher's two working-dialog callbacks, bound to `gui_queue`.

    Both carry the launch generation the message belongs to. One dialog
    is shared by every provider, so the GUI has to know which launch a
    dismiss is for before it acts on it -- and the queue below is what
    orders a replacement's show against a replaced launch's hide, which
    is why the decision is made there and not in the launcher
    (wh-launch-addressed-notices).

    Module-level, and returning the pair rather than closing over them
    inside the startup coroutine, so a test can reach them. Both drop
    their message rather than raise: they run on the launcher's monitor
    threads, and a full queue must not end a startup.

    Args:
        gui_queue: The Logic-to-GUI queue.

    Returns:
        The (show, hide) pair for `set_working_callback`.
    """

    def show_working(message: str, generation: int | None = None) -> None:
        try:
            gui_queue.put_nowait({
                "action": "show_working",
                "message": message,
                "owner": launch_owner_token(generation),
            })
        except Exception:
            pass

    def hide_working(generation: int | None = None) -> None:
        try:
            gui_queue.put_nowait({
                "action": "hide_working",
                "owner": launch_owner_token(generation),
            })
        except Exception:
            pass

    return show_working, hide_working


def _reconcile_stopped_remote_provider(
    state_manager,
    remote_launcher,
    provider_name: str,
    *,
    may_wait: bool,
    generation: int | None = None,
) -> None:
    """Record what is running after a remote provider stopped.

    `start_provider` returns True as soon as the child is spawned, so a
    switch records the replacement while it is still starting. When that
    replacement fails later -- a reported startup_failed, or a dead
    subprocess at the ready-signal timeout -- the monitor names the
    replacement, which is exactly the name in the record, so the guard
    inside `set_remote_stt_stopped` does not stop the clear. The engine
    the switch replaced can still be running: `stop_provider` only
    broadcasts the shutdown command, a provider whose socket is
    disconnected never receives it, and providers reconnect on their own.
    Blanking the display then makes the same false claim from the other
    direction, and nothing records a provider on reconnect, so it lasts
    (wh-remote-stt-robustness.2.5).

    So name whatever is still running instead. The wait is here for the
    same reason it is in `_switch_stt_provider`: right after the shutdown
    command the old engine is normally still alive for a moment, and
    deciding on the first answer would record an engine that is about to
    exit.

    Only the startup-monitor thread may wait, and it says so through
    may_wait. That thread has no work left after this call. The other
    caller is start_provider's own exception handler, which reports on
    whatever thread called it, and a switch calls start_provider on the
    event loop -- waiting there would stop every GUI command and every
    voice command for as long as the wait lasts. Nothing is lost by not
    waiting on that path: the record still names the old engine, so the
    guard inside set_remote_stt_stopped ignores the report, and
    _switch_stt_provider's own failure branch does the bounded wait and
    decides (wh-remote-stt-robustness.2.6).

    Both writes below compare the launch generation as well as the
    name. A name cannot tell the launch that failed from a later launch
    of the same provider, and the user can switch away from
    provider_name and back to it while this function runs. Either the
    switch back lands during the bounded wait, so the compare-and-set
    succeeds on the name and records the stale survivor over the new
    launch, or it lands after a refusal, and the fall-through at the end
    clears the new launch. The generation closes both
    (wh-launch-generation, from the ruling on
    wh-remote-stt-robustness.2.8).

    This must not build a state dictionary -- the three-second
    periodic_state_updater carries the correction to the GUI.

    Args:
        generation: The launch of provider_name whose failure started
            this. None when the caller cannot say, in which case both
            writes compare the name alone, as they did before.
    """
    import time

    if not may_wait:
        state_manager.set_remote_stt_stopped(provider_name, generation)
        return

    survivor = None
    waited = 0.0
    try:
        while True:
            survivor = None
            for provider in remote_launcher.get_providers():
                name = provider.get("name")
                if not name or name == provider_name:
                    # is_running answers True while a launch is in
                    # flight and while a child is still exiting, so the
                    # provider that just failed would nominate itself.
                    continue
                if remote_launcher.is_running(name):
                    survivor = name
                    break
            if survivor is None or waited >= _PROVIDER_EXIT_WAIT_S:
                break
            time.sleep(0.2)
            waited += 0.2
    except Exception as e:
        # This thread is the only place that learns a start failed. An
        # unreadable provider list must not cost the stopped state too.
        logger.warning(f"Could not check for a surviving speech engine: {e}")
        survivor = None

    if survivor is not None:
        logger.warning(
            f"{provider_name} stopped, but {survivor} is still running; "
            "recording it as the engine in use"
        )
        # The scan and the wait above ran outside the record lock, so
        # this write is refused when a switch recorded a newer engine
        # meanwhile -- under a different name
        # (wh-remote-stt-robustness.2.7), or under the same name as a
        # later launch of it (wh-launch-generation).
        if state_manager.replace_stopped_remote_stt_provider(
            provider_name, survivor, generation
        ):
            # The survivor is the recorded engine now: watch it.
            remote_launcher.rearm_watchdog(survivor)
            return
        logger.info(
            f"A newer engine was recorded while {provider_name} was being "
            f"reconciled; leaving it rather than naming {survivor}"
        )
    # The guard inside this one ignores a report that names an engine
    # other than the recorded one, and a report about an earlier launch
    # of the recorded engine, so a refused survivor write above cannot
    # blank a newer record under either a different name or the same one
    # (wh-remote-stt-robustness.2.7, wh-launch-generation).
    state_manager.set_remote_stt_stopped(provider_name, generation)


# What the user is told when a settings write is refused because the
# file holds a value where a section belongs (wh-remote-stt-robustness).
_MALFORMED_SETTINGS_MESSAGE = (
    'Your settings file has a value where the STT section belongs, so '
    'this change could not be saved. Nothing was changed.'
)


def _send_update_notice(state_manager, version: str, tag: str) -> None:
    """Ask the GUI to show the update notice (wh-release-update-check).

    update_check.run_update_check calls this through its notify argument
    when a newer release exists. One ``update_available`` action goes on
    the Logic -> GUI queue. A failed put is NOT caught: the module logs the
    exception and tries the notice again at the next check. This function
    writes no record. The GUI process reports ``update_notice_shown`` when
    the notice becomes visible, and _handle_update_notice_shown writes the
    record then (wh-blocked-mic-message.1.2).
    """
    state_manager.state_to_gui_queue.put_nowait({
        "action": "update_available",
        "version": version,
        "tag": tag,
    })


UPDATE_FAILED_ACTION = "update_failed"


def _send_update_failed_notice(state_manager) -> None:
    """Ask the GUI to show that the update could not start.

    One ``update_failed`` action goes on the Logic -> GUI queue. A failed
    put is caught and logged: the caller runs in a task, and an exception
    that leaves such a task shuts Wheelhouse down (wh-release-update-check).
    """
    try:
        state_manager.state_to_gui_queue.put_nowait({"action": UPDATE_FAILED_ACTION})
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Could not send the update failure notice to the GUI: {exc}")


def _store_setting(self, key: str, value) -> bool:
    """Write one setting in memory, refusing a malformed section instead
    of letting it close the program (wh-remote-stt-robustness).

    ConfigService.set walks a dotted key and assigns into whatever each
    level holds. A settings file that carries a plain value where a
    section belongs -- `stt = "whisper"` instead of a `[stt]` table --
    is valid TOML, and ConfigService.get reads through it as a missing
    key, so the program starts and transcribes normally. set() cannot:
    it reaches the scalar and raises TypeError, either on the item
    assignment or on the membership test that precedes it.

    The STT settings commands run as tasks created by
    create_task_with_error_handling, and _handle_task_completion turns
    any exception other than CancelledError into request_shutdown. So
    without this guard one ordinary voice command against such a
    settings file closes the whole program. ServiceManager.start_remote_stt
    already guards its own repair write against the same failure.

    Returns True when the value was stored and False when the settings
    file cannot hold it; the caller reports the refusal to the user and
    leaves the setting alone.

    Module-level, taking self, for the same reason as the lifecycle-lock
    helper below: the command tests call these commands unbound on a
    MagicMock(spec=LogicController), where a method call would return a
    mock instead of running this code.
    """
    try:
        self.config_service.set(key, value)
        return True
    except TypeError as e:
        logger.error(
            f'Could not store {key}: the settings file holds a value where '
            f'a section belongs ({e})'
        )
        return False


def _serialized_stt_lifecycle(func):
    """Serialize the STT lifecycle commands behind one shared lock
    (wh-google-creds-file-picker.1.3).

    The GUI listener starts each command as its own asyncio task, and
    _switch_stt_provider awaits for up to _PROVIDER_EXIT_WAIT_S between
    stopping the old provider and starting the replacement process.
    Without this lock a second provider switch, or a restart command,
    could run inside that window and leave two provider processes
    running or none. The lock is created lazily (no await between the
    check and the assignment, so tasks cannot race the creation on the
    single-threaded loop): tests build the controller as a spec'd mock
    that never runs __init__.
    """
    @functools.wraps(func)
    async def wrapper(self, *args, **kwargs):
        async with _get_stt_lifecycle_lock(self):
            return await func(self, *args, **kwargs)
    return wrapper


def _get_stt_lifecycle_lock(self):
    """The shared STT lifecycle lock, created lazily (no await between
    the check and the assignment, so tasks cannot race the creation on
    the single-threaded loop): tests build the controller as a spec'd
    mock that never runs __init__."""
    lock = getattr(self, '_stt_lifecycle_lock', None)
    if lock is None:
        lock = self._stt_lifecycle_lock = asyncio.Lock()
    return lock


# The Wheelhouse Assistant's address: the chat view of a Gemini Notebook. This
# is the same address config.toml.example ships as the assistant_url default,
# and test_the_assistant_constant_equals_the_shipped_default keeps the two in
# step. LogicController.start_help_online does not read this constant: it
# opens the address in the [ai.help] assistant_url setting as written.
_WHEELHOUSE_ASSISTANT_URL = (
    "https://notebook.google.com/notebook/"
    "da51a404-67ec-4804-9ebe-83605df3e9cf/preview"
)


def default_help_explainer_marker_path() -> Path:
    """Return the file that records a choice in the explanation window.

    A user who ticked "Do not show this again" in 1.2.0 holds
    ai.help.explain_before_open = false. The assistant then moved to a
    Gemini Notebook, which takes only a Google Account, and only the
    explanation window says so (wh-assistant-explainer-once-more). While
    this file is missing, start_help_online shows the window once more to
    such a user. The Assistant button in the window writes the file. It
    lives beside click_first_use_hint_shown.toml and uses the same reader
    and writer, so config.toml is never written for this step.
    """
    return (
        Path(__file__).resolve().parent
        / "data"
        / "help_explainer_notebook_shown.toml"
    )


def default_first_run_notice_marker_path() -> Path:
    """Return the file that records the first-run notice as shown.

    The Logic process reads this file once at start-up. While it is
    missing, this is the first start after an install: the GUI is asked to
    show the first-run notice, and the file is then written, so later
    starts show nothing (wh-first-run-notice). It lives beside
    help_explainer_notebook_shown.toml and uses the same reader and writer
    (click_first_use_hint.load_hint_shown and mark_hint_shown), so
    config.toml is never written for this step.
    """
    return (
        Path(__file__).resolve().parent
        / "data"
        / "first_run_notice_shown.toml"
    )


# The notice for a choice in the explanation window that was not kept. It
# says "can appear again": with no marker, and a setting that is true or
# unchanged from false, the next Help shows the window.
_HELP_CHOICE_NOT_SAVED_NOTICE = (
    "Wheelhouse could not save your choice from the Assistant window. "
    "The window can appear again when you choose Help."
)


async def _record_help_explainer_choice(
    config, state_manager, do_not_show_again: bool
) -> str | None:
    """Record the choice made with the window's Assistant button. Never raises.

    Returns None when the choice is kept, or the text of a notice for the
    user when a write failed; the caller shows it. Before the choice moved
    to Logic, the GUI sent this save through its acknowledged settings path,
    which shows a notice on failure (wh-assistant-explainer-once-more.1.1).

    The setting is saved first, and only when its value on disk differs
    from the choice: every ConfigService.save rewrites the whole settings
    file and drops the user's comments, so a ticked box with the setting
    already false saves nothing. An unticked box turns the window back on,
    which is what the check box says. The marker is written only after that
    save succeeds, or when no save is needed: a marker written before a
    failed save would keep a false setting the user asked to change, and
    the window that offers the choice would not be shown again.

    The comparison reads the value on disk (get_persisted), not the live
    value, and the save is staged (save(values=...)), which changes the
    live value only when the file was replaced. A save that changed the
    live value first and then failed left the live value matching the
    choice while the file did not; a second press in the same process then
    saved nothing and wrote the marker, and after a restart the file's old
    value plus the marker hid the choice for good (Codex round 2 on
    wh-assistant-explainer-once-more.1.1).
    """
    from services.wheelhouse.click_first_use_hint import (
        load_hint_shown,
        mark_hint_shown,
    )

    key = "ai.help.explain_before_open"
    wanted = not do_not_show_again
    saved = False
    if state_manager is None:
        saved = bool(config.get_persisted(key, True)) == wanted
        if not saved:
            logger.warning(
                "Help: no state manager, the window setting was not saved."
            )
    else:
        try:
            # The staged save StateManager._save_gui_settings makes for a
            # GUI settings write, under the same lock, without its request
            # ID: no GUI request waits for an acknowledgement here, so the
            # notice returned below takes its place. The comparison is made
            # under the lock too: the window hides at once, so a second
            # choice can arrive while an earlier save is pending, and until
            # that save ends the value on disk is the old one
            # (wh-assistant-explainer-once-more.1.3).
            if not hasattr(state_manager, "_gui_settings_lock"):
                state_manager._gui_settings_lock = asyncio.Lock()
            async with state_manager._gui_settings_lock:
                if bool(config.get_persisted(key, True)) == wanted:
                    saved = True
                else:
                    try:
                        saved = bool(await config.save(values={key: wanted}))
                        if not saved:
                            logger.warning(
                                "Help: the window setting was not saved to disk."
                            )
                    except Exception as exc:
                        logger.warning(
                            "Help: the window setting could not be saved: %s", exc
                        )
                    state_manager.send_state_update()
        except Exception as exc:
            logger.warning("Help: the window setting save did not finish: %s", exc)
    if not saved:
        # No marker: the choice is not complete, so the next Help shows
        # the window again and the user can choose again.
        return _HELP_CHOICE_NOT_SAVED_NOTICE

    try:
        marker = default_help_explainer_marker_path()
        if not await asyncio.to_thread(load_hint_shown, marker):
            if not await asyncio.to_thread(mark_hint_shown, marker):
                logger.warning("Help: the explanation window marker was not written.")
                return _HELP_CHOICE_NOT_SAVED_NOTICE
    except Exception as exc:
        logger.warning("Help: the explanation window marker failed: %s", exc)
        return _HELP_CHOICE_NOT_SAVED_NOTICE
    return None


# wh-first-sound-lost.23: the lw_* actions of the 'Your training word' window.
# Module level, so a test that calls _build_gui_handler_map on a mock still
# finds the list.
_LEAD_WORD_GUI_ACTIONS = (
    "lw_open", "lw_close", "lw_record", "lw_keep", "lw_discard",
    "lw_restore", "lw_off", "lw_on", "lw_play_start", "lw_play_end",
)


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
        # True while a manual update check runs (wh-about-check-updates).
        # A second check_for_updates command then sends no second request.
        self._manual_update_check_running = False
        # True while Update now downloads and starts the installer. The
        # update notice and the About window both send update_now, so a
        # second request then starts nothing (wh-about-check-updates).
        self._update_now_running = False
        # Set once a speech provider of this start reports ready. The
        # first-run notice waits for it (wh-first-run-notice.2).
        self._speech_provider_ready = asyncio.Event()

        # wh-overlay-slow-uia-stale-badges.7: ``time.monotonic()`` stamp
        # set while an Input-side screen read (an overlay build walk or a
        # by-name click_element walk) is awaited, None otherwise. The
        # speech processor's dictation gate reads it and refuses new
        # dictation words while it is set: the read blocks Input's ONE
        # command loop, so a word sent now would type seconds late. Set
        # and cleared in a try/finally around the send awaits; the gate
        # carries its own maximum wait so a destroyed awaiter cannot
        # refuse voice forever. Pin/unpin bookkeeping deliberately does
        # not set it -- those requests run no UIA walk.
        self.screen_read_in_flight_since: Optional[float] = None
        # wh-overlay-slow-uia-stale-badges.7.1.3: how many screen reads
        # are in flight RIGHT NOW. The overlay build walk and the
        # by-name click walk can overlap (forward_click_element takes
        # no lock), and with a single slot the first finisher would
        # clear the stamp while the second walk still blocks Input's
        # command loop. _mark_screen_read_started/_finished keep the
        # stamp alive until the LAST walk finishes.
        self._screen_reads_in_flight: int = 0

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

        # 'Your training word' window controller, built on first use
        # (wh-first-sound-lost.23, see _get_lead_word_controller).
        self._lead_word_controller = None

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

        # wh-tab7j (wh-l4h.1 Phase 1): validated voice-clicking config.
        # ClickConfig.from_raw is the only never-raising reader of the [click]
        # block; read it once at init so the click_element action gate and the
        # awaiter both see one config. The snapshot-summary cache is not built
        # here: NumberedOverlayController.__init__ builds it from this config.
        # The one-shot disabled-by-config notice is suppressed after the first
        # attempt per session (_click_disabled_notice_shown, below).
        from services.wheelhouse.ui.click_config import ClickConfig

        self.click_config = ClickConfig.from_raw(
            self.config_service.get("click", {})
        )
        # wh-overlay-refactor-pilot.2: the numbered overlay and voice clicking
        # live in NumberedOverlayController; it reads the members above
        # through its back-reference to this controller.
        self.overlay = NumberedOverlayController(self, clock=time.monotonic)
        self._click_disabled_notice_shown: bool = False

        # wh-grid-speech-routing: Logic-side mouse-grid state machine
        # (grid_overlay_state.py). Pure FSM; this controller performs its
        # effects (paint/pin/clear to the GUI, click/move/drag to Input,
        # notices) in MouseGridController (mouse_grid/controller.py),
        # handle_grid_command and _perform_grid_effects. Sized
        # from the validated [click] config so a bad key degrades to the
        # defaults instead of crashing startup. Mutual exclusion with the
        # numbered overlay is wired both ways: opening the grid performs
        # CLOSE_NUMBERED_OVERLAY, and the show/auto-open paths of
        # NumberedOverlayController call its _close_grid_for_numbered_overlay.
        from services.wheelhouse.grid_overlay_state import (
            GridOverlayStateMachine,
        )

        self.grid_overlay_state = GridOverlayStateMachine(
            grid_min_cell_px=self.click_config.grid_min_cell_px,
            drag_duration_ms=self.click_config.drag_duration_ms,
        )
        # wh-full-refactor.6.2: the mouse grid commands and the pointer
        # actions (area L5) live in MouseGridController; it reads the
        # members above through its back-reference to this controller.
        self.mouse_grid = MouseGridController(self)

        # One lock for every STT lifecycle command (provider switch,
        # service restart); acquired by the
        # ``_serialized_stt_lifecycle`` decorator so no two of them
        # interleave their read-config/stop/start steps
        # (wh-google-creds-file-picker.1.3).
        self._stt_lifecycle_lock = asyncio.Lock()

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
        WheelHouseApp demux/sender loops), and a Task that died
        unawaited means that work is gone and the app is silently
        degraded -- a dead broadcast loop is worse than a
        launcher-supervised restart. Task failures created via
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

    def _startup_task_coroutines(self) -> dict:
        """Name each background coroutine that main() starts after setup.

        main() creates every entry as a task through
        create_task_with_error_handling. UpdateCheck sleeps at least 60
        seconds before its first request and runs its request in a worker
        thread, so it never delays the other tasks. It writes no notice
        record itself: the record is written by _handle_update_notice_shown
        when the GUI process reports that the notice is on the screen
        (wh-release-update-check, wh-blocked-mic-message.1.2).
        """
        return {
            "PeriodicStateUpdate": self.periodic_state_updater(),
            "UpdateCheck": self._run_update_check(),
            "FirstRunNotice": self._run_first_run_notice(),
        }

    async def _run_first_run_notice(self) -> None:
        """Show the first-run notice on the first start after an install.

        Reads the marker once. When it says the notice was not shown (a
        missing file does; load_hint_shown reads an unreadable file as
        shown, so a damaged marker never repeats the notice), the GUI is
        sent one ``open_first_run_notice`` message. This method does not
        write the marker. A successful queue put proves only that the
        message reached the transport, not that the GUI process read it or
        showed a window. The GUI process sends a ``first_run_notice_shown``
        command once the window is on screen, and
        _handle_first_run_notice_shown writes the marker then. A start whose
        GUI process never shows the window therefore leaves the marker
        unwritten, and the next start tries again. The accepted trade-off
        runs the other way: a window that appears while its command is
        lost shows the notice once more on the next start
        (wh-first-run-notice.1.1).

        After the marker read, the method waits with no time limit until a
        speech provider of this start reports ready (_speech_provider_ready,
        set by _speech_provider_ready_callback). The notice used to open
        under the loading window while the provider was still loading
        (wh-first-run-notice.2). A start that never becomes ready sends
        nothing, so the marker stays unwritten and the next start tries
        again. Shutdown cancels the wait; the cancel is not an Exception,
        so it passes the handler below and _handle_task_completion logs it
        as a cancel. A start that finds the marker ends before the wait.

        The marker read runs in a worker thread, off the event loop. This
        method catches every exception: an exception that escapes a task
        made by create_task_with_error_handling shuts Wheelhouse down.
        """
        try:
            from services.wheelhouse.click_first_use_hint import load_hint_shown

            marker = default_first_run_notice_marker_path()
            if await asyncio.to_thread(load_hint_shown, marker):
                return
            await self._speech_provider_ready.wait()
            state_manager = getattr(self, "state_manager", None)
            queue = getattr(state_manager, "state_to_gui_queue", None)
            if queue is None:
                logger.warning("First-run notice: no GUI queue, not shown.")
                return
            try:
                queue.put_nowait({"action": "open_first_run_notice"})
            except Exception as exc:
                logger.warning("First-run notice could not be queued: %s", exc)
                return
        except Exception as exc:
            logger.warning("First-run notice failed: %s", exc)

    def _speech_provider_ready_callback(self) -> "Callable[[], None]":
        """Build the callback that RemoteSTTLauncher calls on an accepted ready.

        Call it on the event loop; it keeps that loop. The launcher calls
        the result on whichever thread delivered the ready, so the result
        hands the set of _speech_provider_ready to the loop with
        call_soon_threadsafe (wh-first-run-notice.2).
        """
        loop = asyncio.get_running_loop()
        ready = self._speech_provider_ready

        def on_ready() -> None:
            loop.call_soon_threadsafe(ready.set)

        return on_ready

    async def _handle_first_run_notice_shown(self) -> None:
        """Write the first-run marker after the GUI process showed the window.

        The GUI process sends ``first_run_notice_shown`` once the notice is
        on screen (GuiManager._open_first_run_notice). A second command
        writes the marker again, which mark_hint_shown allows. The write
        runs in a worker thread, off the event loop. This method catches
        every exception: an exception that escapes a task made by
        create_task_with_error_handling shuts Wheelhouse down
        (wh-first-run-notice.1.1).
        """
        try:
            from services.wheelhouse.click_first_use_hint import mark_hint_shown

            marker = default_first_run_notice_marker_path()
            if not await asyncio.to_thread(mark_hint_shown, marker):
                logger.warning("First-run notice: the marker was not written.")
        except Exception as exc:
            logger.warning("First-run notice marker failed: %s", exc)

    async def _handle_update_notice_shown(self, version) -> None:
        """Write the update-notice record after the GUI process showed the notice.

        The GUI process sends ``update_notice_shown`` with the notice's
        version at the one moment the update notice becomes visible
        (GuiManager._stack_update_notice). A notice that waits for a place
        on the screen sends nothing, so no record hides a version the user
        never saw (wh-blocked-mic-message.1.2). A command without a version
        text writes nothing. A second command writes the record again. The
        write runs in a worker thread, off the event loop. This method
        catches every exception: an exception that escapes a task made by
        create_task_with_error_handling shuts Wheelhouse down.
        """
        try:
            if not (isinstance(version, str) and version):
                logger.warning(
                    "update_notice_shown dropped, version missing: %r", version
                )
                return
            if not await asyncio.to_thread(
                update_check.record_notice_shown,
                update_check.state_file_path(),
                version,
                time.time(),
            ):
                logger.warning(
                    "Update notice: could not record the shown notice, so the "
                    "notice for %s can appear again at the next check.",
                    version,
                )
        except Exception as exc:
            logger.warning("Update notice record failed: %s", exc)

    async def _run_update_check(self) -> None:
        """Run the background update check until the task is cancelled.

        The module catches every exception, because an exception that
        escapes a task created by create_task_with_error_handling shuts
        Wheelhouse down. notify puts the notice on the GUI queue.
        """
        await update_check.run_update_check(
            self.config_service,
            lambda version, tag: _send_update_notice(
                self.state_manager, version, tag
            ),
        )

    def _start_manual_update_check(self):
        """Start one manual update check, unless one runs (wh-about-check-updates).

        The About Wheelhouse window's "Check for updates" button and the
        voice command "check for updates" both arrive here as the GUI
        command ``check_for_updates``. The flag is set before the task is
        created, so a second command that arrives before the first task
        runs is also refused. Returns the task, or None when a check runs.
        """
        if self._manual_update_check_running:
            logger.info("Manual update check already running; command ignored")
            return None
        self._manual_update_check_running = True
        try:
            return self.create_task_with_error_handling(
                self._run_manual_update_check(), "ManualUpdateCheck"
            )
        except BaseException:
            self._manual_update_check_running = False
            raise

    async def _run_manual_update_check(self) -> None:
        """Run update_check.check_now off the event loop and send its result.

        The request runs in a worker thread (asyncio.to_thread), so the
        event loop, and with it the GUI, keeps working while it waits. One
        ``update_check_result`` action goes to the GUI process, which shows
        it in the About Wheelhouse window. An unexpected failure still sends
        a "failed" result, so the window never waits for ever. This method
        catches every exception: an exception that escapes a task made by
        create_task_with_error_handling shuts Wheelhouse down.
        """
        message = {
            "action": "update_check_result",
            "status": "failed",
            "installed": "",
            "version": "",
            "tag": "",
        }
        try:
            result = await asyncio.to_thread(update_check.check_now)
            message.update(
                status=result.status,
                installed=result.installed,
                version=result.version,
                tag=result.tag,
            )
        except Exception as exc:  # noqa: BLE001 -- the window must get an answer
            logger.warning(f"Manual update check failed: {exc}")
        finally:
            self._manual_update_check_running = False
        try:
            self.state_manager.state_to_gui_queue.put_nowait(message)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Could not send the update check result to the GUI: {exc}")

    async def _handle_update_now(self, version, tag) -> None:
        """Update now on the update notice: download the installer for ``tag``, then run it.

        The order is the point of this method:

        1. Download the installer script of the release ``tag`` and check
           its digest (update_install.prepare_update). The download blocks,
           so it runs in a worker thread, as the update check does
           (update_check.run_update_check). Nothing else has happened yet.
        2. Start the launch console (stt/parakeet_model.py
           launch_installer) with the installer parameters of the current
           install. The console waits for every Wheelhouse process to exit
           and only then runs the installer.
        3. Leave through the shutdown that the tray's Exit and the Parakeet
           offer use (request_shutdown).

        A failure in step 1 or 2 closes nothing and starts nothing. It
        writes one warning that names the version and the reason, and sends
        one ``update_failed`` action so the GUI shows a notice. This method
        catches every exception: an exception that leaves a task made by
        create_task_with_error_handling shuts Wheelhouse down.
        """
        if self.shutdown_event and self.shutdown_event.is_set():
            logger.info("Update now ignored: Wheelhouse is already shutting down")
            return
        if self._update_now_running:
            logger.info(f"Update now ignored for version {version!r}: an update already runs")
            return
        # Each failure return below clears the flag. A success shuts
        # Wheelhouse down, so the flag stays set until the process exits.
        self._update_now_running = True
        logger.info(f"Update now requested for version {version!r}, release tag {tag!r}")
        try:
            prepared = await asyncio.to_thread(
                update_install.prepare_update, tag, update_check.get_app_version()
            )
            # The runtime record comes first: a scalar ``stt = "..."`` config
            # runs fine but has no stt.last_provider (wh-release-update-check.1.3).
            running_engine = self.state_manager.get_running_remote_stt_provider()
            parameters = await asyncio.to_thread(
                update_install.build_installer_parameters,
                self.config_service,
                running_engine=running_engine,
            )
            pids = self._wheelhouse_process_ids()
            started = await asyncio.to_thread(
                parakeet_model.launch_installer,
                prepared.script_path,
                pids,
                installer_parameters=parameters,
            )
        except Exception as exc:  # noqa: BLE001 -- any failure leaves Wheelhouse running
            logger.warning(f"Update to version {version!r} failed: {exc}")
            self._update_now_running = False
            _send_update_failed_notice(self.state_manager)
            return
        if not started:
            # crewcut: the downloaded folder stays in the temporary folder
            # after a failed start, and it also stays when a step after the
            # download raises, which the except branch above catches. Remove
            # it in both places with shutil.rmtree.
            logger.warning(
                f"Update to version {version!r} failed: the installer console did not start"
            )
            self._update_now_running = False
            _send_update_failed_notice(self.state_manager)
            return
        logger.info(
            f"Started the Wheelhouse installer for version {version!r}; "
            "shutting down so it can run"
        )
        self.request_shutdown()

    async def _handle_parakeet_download_now(self) -> None:
        """Download now on the Parakeet model offer: start the installer, then exit.

        The installer refuses to run while Wheelhouse runs, so the launch
        console (stt/parakeet_model.py launch_installer) first waits for
        every Wheelhouse process to exit, then runs the Setup program's
        installer with Parakeet chosen. This process starts that console
        and then leaves through the same shutdown the tray's Exit starts:
        the shared shutdown event (request_shutdown). With no restart flag
        written, the launcher exits instead of restarting.

        The locator is asked again here, because the Setup copy can have
        changed since the offer was shown. If it now answers with the
        one-line command, or the console does not start, the fallback
        offer is shown instead and Wheelhouse keeps running
        (wh-parakeet-model-download-offer A2).
        """
        if self.shutdown_event and self.shutdown_event.is_set():
            logger.info("Download now ignored: Wheelhouse is already shutting down")
            return
        plan = parakeet_model.locate_installer()
        if not isinstance(plan, parakeet_model.InstallerLaunchPlan):
            logger.info(
                "Download now: no installer to start "
                f"({getattr(plan, 'reason', 'unknown')}); showing the install command"
            )
            parakeet_model.send_parakeet_model_offer(self.state_manager, plan)
            return
        pids = self._wheelhouse_process_ids()
        started = await asyncio.to_thread(
            parakeet_model.launch_installer, plan.script_path, pids,
        )
        if not started:
            logger.warning(
                "The Wheelhouse installer did not start; showing the install "
                "command instead"
            )
            parakeet_model.send_parakeet_model_offer(
                self.state_manager,
                parakeet_model.InstallerFallback(
                    command=parakeet_model.ONE_LINE_INSTALL_COMMAND,
                    reason="launch_failed",
                ),
            )
            return
        logger.info(
            "Started the Wheelhouse installer for the Parakeet model; "
            "shutting down so it can run"
        )
        self.request_shutdown()

    def _wheelhouse_process_ids(self) -> list[int]:
        """Process ids of every Wheelhouse process this process can name.

        This Logic process; its parent, the launcher (launcher.py starts
        Logic, Input and GUI as its multiprocessing children); the
        launcher's children, which are Input and GUI; and the running
        speech providers (RemoteSTTLauncher.running_provider_pids). The
        launch console waits for all of them, and then also for any
        python.exe, pythonw.exe or uv.exe whose command line holds the app
        folder, which covers anything this list misses. Never raises.
        """
        pids: list = [os.getpid()]
        try:
            import psutil

            parent = psutil.Process(os.getpid()).parent()
            if parent is not None:
                pids.append(parent.pid)
                pids.extend(child.pid for child in parent.children())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not list the Wheelhouse processes: {exc}")
        launcher = getattr(self.service_manager, "remote_stt_launcher", None)
        if launcher is not None:
            try:
                pids.extend(launcher.running_provider_pids())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Could not list the speech provider processes: {exc}")
        result: list[int] = []
        for pid in pids:
            if (isinstance(pid, int) and not isinstance(pid, bool)
                    and pid > 0 and pid not in result):
                result.append(pid)
        return result

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

    @_serialized_stt_lifecycle
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

            # Save the new value so the next start reads it
            # (wh-voice-settings-window). set_config_value also sends a state
            # update; the one below repeats the same snapshot and is harmless.
            await self.state_manager.set_config_value('INTERIM_RESULTS', enabled)

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

    @_serialized_stt_lifecycle
    async def _switch_stt_provider(self, provider: str) -> None:
        """Switch to a different remote provider: stop the old, start the new.

        wh-in-process-capture-removal deleted the two other paths this
        method had. One handed the change to STTManager, the engine that
        ran inside this process. The other was the mode change: a provider
        no launcher knew was treated as an in-process one, which wrote
        stt.mode into the settings file and restarted the program into a
        mode that no longer exists. Such a name now reaches the remote
        start like any other and fails there, and the failure arm below
        is what tells the user.
        """
        def notify(title: str, message: str) -> None:
            try:
                self.state_manager.state_to_gui_queue.put_nowait({
                    'action': 'show_notification',
                    'title': title,
                    'message': message,
                    'timeout': 5,
                })
            except Exception:
                pass

        def drop_stale_bare_number_hold() -> None:
            """Drop any bare number the old engine left held.

            The speech processor outlives both engines, so a hold armed
            from the outgoing engine's words is still standing when the
            new one starts and gets typed as dictation by its first word
            (wh-provider-switch-stale-hold).

            Called from the two places where the switch has
            already established that the outgoing engine cannot complete
            the hold, and from nowhere else. Both ask
            outgoing_engine_is_gone below and drop only when it says the
            outgoing process exited.

            Two things are deliberately NOT the trigger, and each was
            tried first. Reaching the stop send is not: stop_provider
            only sends the shutdown command, returns False when that send
            raises, and this method's caller discards the answer, so a
            stop that never landed reads the same as one that did
            (wh-provider-switch-stale-hold.1.1). start_provider returning
            True is not either: it returns on a successful spawn, and the
            replacement can fail its own startup afterwards while the
            engine it replaced keeps transcribing
            (wh-provider-switch-stale-hold.2.1). Both mistakes have the
            same shape and cost the same thing -- the old engine is still
            the user's engine, the user is mid-number, and the number is
            thrown away to fix a staleness that has not happened. Only
            the exit answers it. Nothing is dropped on the early return
            above either, where the requested provider is the one already
            running: no engine changes there.

            The chain is Optional at both hops -- ServiceManager sets
            speech_handler during service initialization
            (service_manager.py, declared None) and SpeechHandler sets
            speech_processor in initialize_speech_processor (declared
            None) -- so it is read the way the overlay sentence wait
            reads it. In a running program both are set before any tray
            or voice command can reach this method; the guard is for
            partially built fixtures and for a switch during startup.
            """
            processor = getattr(
                getattr(
                    getattr(self, "service_manager", None),
                    "speech_handler", None,
                ),
                "speech_processor", None,
            )
            if processor is not None:
                processor.drop_bare_number_hold("stt provider switch")

        async def outgoing_engine_is_gone(launcher, name: str) -> bool:
            """Wait out the shutdown command; say whether `name` exited.

            The only proof this method can get that the engine which
            armed the hold is finished. start_provider returning True is
            not that proof: it returns as soon as subprocess.Popen
            succeeds (stt/remote_stt_launcher.py:1426-1460), readiness is
            decided later on a monitor thread, and the same True comes
            back when the requested provider was already running
            (:1310, :1319). _reconcile_stopped_remote_provider's
            docstring above says what follows -- the replacement can fail
            its startup afterwards while the engine it replaced is still
            running (wh-provider-switch-stale-hold.2.1).

            Bounded, and polled rather than awaited on an event, because
            no exit signal exists to await: the launcher learns of an
            exit only by asking. Right after the shutdown command the old
            engine is normally alive for a moment, so a single question
            would answer "still running" on nearly every successful
            switch; the loop is what makes the ordinary case come out
            right. Both arms of the remote branch use it, so neither can
            drift from the other.
            """
            waited = 0.0
            still_running = launcher.is_running(name)
            while still_running and waited < _PROVIDER_EXIT_WAIT_S:
                await asyncio.sleep(0.2)
                waited += 0.2
                still_running = launcher.is_running(name)
            return not still_running

        # The tray's read path: the runtime record of the engine that
        # actually started, config as fallback. Reading config directly
        # here would stop the wrong engine after a fallback whose repair
        # write failed (provider-removal review .1.9).
        current_provider = self.state_manager._get_current_stt_provider()

        remote_launcher = getattr(self.service_manager, 'remote_stt_launcher', None)

        # Check if we're already on the requested provider
        if provider == current_provider:
            logger.debug(f"Already using provider {provider}, no switch needed")
            return

        # Parakeet without its complete speech model cannot start. Offer
        # the installer instead and change nothing: no engine is stopped,
        # no setting is written, the current engine stays the user's
        # engine (wh-parakeet-model-download-offer A1, A2). The model is
        # checked at every pick, because it re-reads the model-path
        # override file (boss ruling b). A complete model takes the
        # ordinary switch below, unchanged (A4).
        if (
            provider == parakeet_model.PROVIDER_NAME
            and not parakeet_model.parakeet_model_complete()
        ):
            plan = parakeet_model.locate_installer()
            logger.info(
                "Parakeet was picked but its speech model is not complete; "
                f"offering the installer ({type(plan).__name__})"
            )
            parakeet_model.send_parakeet_model_offer(self.state_manager, plan)
            return

        if remote_launcher:
            # Different provider: stop old, start new
            logger.info(f"Switching remote STT provider: {current_provider} -> {provider}")

            # Stop current provider (ignore failures - may already be stopped)
            await remote_launcher.stop_provider(current_provider)

            # Start new provider
            if remote_launcher.start_provider(provider):
                # Keep the runtime record current, or the GUI would keep
                # showing the startup-time provider after this switch.
                self.state_manager.set_running_remote_stt_provider(provider)
                # The engine has already changed by this point, so
                # only the record of it can fail here: report the
                # refusal and leave the running engine alone
                # (wh-remote-stt-robustness).
                stored = _store_setting(self, "stt.last_provider", provider)
                saved = stored and await self.config_service.save()
                self.state_manager.send_state_update()
                if saved:
                    logger.info(f"Switched to remote STT provider: {provider}")
                elif not stored:
                    logger.error(
                        f"Switched to remote STT provider {provider}, but "
                        "the settings file could not record the choice"
                    )
                    notify(
                        'Wheelhouse: STT Choice Not Saved',
                        _MALFORMED_SETTINGS_MESSAGE,
                    )
                else:
                    # The switch itself worked; only the record of it failed,
                    # so the next start will come back on the old provider.
                    logger.error(
                        f"Switched to remote STT provider {provider}, but the "
                        "choice was NOT saved and will not survive a restart"
                    )
                # LAST, after every effect the user can see. The
                # record, the save and the state update all happen at
                # the same moment they always did; only the hold drop
                # waits, and nobody can see a hold. Dropping on the
                # spawn instead threw the number away in the cell
                # where the shutdown never landed and the replacement
                # then failed its own startup -- the old engine is
                # still the user's engine there, and its next word
                # still completes the number
                # (wh-provider-switch-stale-hold.2.1).
                if await outgoing_engine_is_gone(
                    remote_launcher, current_provider,
                ):
                    drop_stale_bare_number_hold()
            else:
                logger.error(f"Failed to start remote STT provider: {provider}")
                # stop_provider only sends the shutdown command over
                # the WebSocket and returns; it does not wait for the
                # old process to exit, and a provider whose socket is
                # disconnected never receives it. Marking the engine
                # stopped while the old one is still transcribing is
                # the same false claim from the other direction, and
                # no path records a provider on reconnect, so it
                # would last. Wait for the exit the way the
                # credentials restart does
                # (wh-remote-stt-robustness.2.2). The wait moved into
                # outgoing_engine_is_gone when the success arm above
                # came to need the same answer
                # (wh-provider-switch-stale-hold.2.1); the behaviour
                # here is unchanged.
                still_running = not await outgoing_engine_is_gone(
                    remote_launcher, current_provider,
                )
                if still_running:
                    # Nothing in this branch cleared the record, so
                    # the engine that is still running is still the
                    # one the reader names; leave it alone and say
                    # what happened.
                    logger.error(
                        f"{provider} did not start and {current_provider} "
                        "did not exit; leaving the old engine running"
                    )
                    notify(
                        'Wheelhouse: STT Switch Failed',
                        f'{provider} could not start. The previous speech '
                        'engine is still running.',
                    )
                    # Still the recorded engine, so keep watching it.
                    remote_launcher.rearm_watchdog(current_provider)
                else:
                    # The old engine was stopped above, before this
                    # one was tried, so nothing is running now.
                    # Without this the tray keeps the check mark on
                    # the engine that stopped
                    # (wh-remote-stt-robustness).
                    #
                    # The exit is confirmed at this point and no new
                    # engine took its place, so the hold can never be
                    # completed and goes with it. The still_running
                    # arm above drops nothing: that engine is still
                    # the user's engine
                    # (wh-provider-switch-stale-hold.1.1).
                    drop_stale_bare_number_hold()
                    self.state_manager.set_remote_stt_stopped()
                self.state_manager.send_state_update()

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
        if await self.config_service.save():
            logger.info("AI model set to: %s", provider)
        else:
            logger.error(
                "AI model set to %s, but the choice was NOT saved and will not "
                "survive a restart", provider
            )

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
                    f"'open voice access help'.]"
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

    # wh-overlay-refactor-pilot.2: the numbered overlay and voice clicking live
    # in NumberedOverlayController (numbered_overlay/controller.py). These
    # four members stay here because other modules call them on the
    # LogicController: speech/actions.py awaits forward_click_element and
    # handle_overlay_command and calls forward_click_notice, and
    # speech/speech_processor.py reads click_overlay_state.

    async def forward_click_element(
        self, query, trace_id: str, *, defer_failure_notice: bool = False,
    ) -> Optional[dict]:
        """Forward a voice 'click <target>' to the overlay controller."""

        return await self.overlay.forward_click_element(
            query, trace_id, defer_failure_notice=defer_failure_notice,
        )

    async def handle_overlay_command(
        self, command: str, trace_id: str,
    ) -> "Optional[asyncio.Task]":
        """Forward a 'show numbers' or 'hide numbers' command to the overlay."""

        return await self.overlay.handle_overlay_command(command, trace_id)

    def forward_click_notice(self, **kwargs) -> None:
        """Forward a click notice to the GUI through the overlay controller.

        speech/actions.py calls this when it shows a deferred click failure.
        """

        self.overlay.forward_click_notice(**kwargs)

    @property
    def click_overlay_state(self):
        """The overlay state machine, read by the speech processor."""

        return self.overlay.click_overlay_state

    # ------------------------------------------------------------------
    # wh-full-refactor.6.2: the mouse grid and the pointer click live in
    # MouseGridController (mouse_grid/controller.py), built in __init__ as
    # self.mouse_grid. These two methods stay because speech/actions.py and
    # the numbered overlay (through numbered_overlay/host.py) call them on
    # LogicController.
    # ------------------------------------------------------------------
    async def handle_pointer_click(
        self, gesture, trace_id: str, *, spoken: str = "",
    ) -> bool:
        """Click at the pointer; calls ``MouseGridController.handle_pointer_click``."""
        return await self.mouse_grid.handle_pointer_click(
            gesture, trace_id, spoken=spoken,
        )

    async def handle_grid_command(
        self, command: str, trace_id: str, *,
        number: int = 0,
        gesture=None,
        spoken: str = "",
    ) -> bool:
        """Apply one grid command; calls ``MouseGridController.handle_grid_command``."""
        return await self.mouse_grid.handle_grid_command(
            command, trace_id, number=number, gesture=gesture, spoken=spoken,
        )

    def _handle_pattern_manager_tree_changed(self, command) -> None:
        """Validate a pattern_manager_tree_changed payload and hand it on.

        wh-overlay-rewalk-after-filter, contract C5. The Pattern Manager sends
        this whenever its tree changes what a walk of the dialog would return.
        ``safe_parse`` logs and returns ``None`` on a malformed payload
        (wh-uf54), so a bad dict is dropped here rather than raised into the
        GUI command listener. The decision -- whether the change earns a
        re-walk -- belongs to ``_on_pattern_manager_tree_change``; this handler
        only validates and delegates.
        """
        from services.wheelhouse.shared.ipc_schema_validation import safe_parse
        from services.wheelhouse.shared.pattern_manager_tree_changed import (
            PatternManagerTreeChangedEvent,
        )

        ev = safe_parse(
            PatternManagerTreeChangedEvent.from_dict,
            command,
            log_label="pattern_manager_tree_changed",
        )
        if ev is None:
            return  # already logged

        self.overlay.on_pattern_manager_tree_change(ev.hwnd, ev.sequence)


    def _mark_screen_read_started(self) -> None:
        """Count a screen read in and stamp the dictation-gate marker.

        (wh-overlay-slow-uia-stale-badges.7.1.3.) Both walk sites (the
        overlay build send and the by-name click_element send) call
        this instead of writing ``screen_read_in_flight_since``
        directly: the walks can overlap, and a raw write would let the
        first finisher's clear hide the second walk from the gate. The
        stamp always holds the NEWEST start, which preserves the
        gate's cap semantics. The isinstance guard keeps legacy
        MagicMock fixtures (which auto-create a Mock attribute) inert.
        """
        count = getattr(self, "_screen_reads_in_flight", 0)
        if not isinstance(count, int) or isinstance(count, bool):
            count = 0
        self._screen_reads_in_flight = count + 1
        self.screen_read_in_flight_since = time.monotonic()

    def _mark_screen_read_finished(self) -> None:
        """Count a screen read out; clear the marker when none remain."""
        count = getattr(self, "_screen_reads_in_flight", 1)
        if not isinstance(count, int) or isinstance(count, bool):
            count = 1
        self._screen_reads_in_flight = max(count - 1, 0)
        if self._screen_reads_in_flight == 0:
            self.screen_read_in_flight_since = None

    # wh-full-refactor.3.1: the public members that the numbered overlay
    # (numbered_overlay/host.py OverlayHost) uses. Each one delegates to a
    # private member or field of this controller, or to MouseGridController.

    def take_click_disabled_notice(self) -> bool:
        """Claim the one-shot voice-clicking-disabled-by-config notice.

        Returns True the first time, and marks the notice shown before it
        returns, so the caller shows the notice once. Returns False after
        that. Reads and sets ``_click_disabled_notice_shown``. The by-name
        click (numbered_overlay/controller.py and speech/actions.py) and
        ``MouseGridController.handle_grid_command`` (the grid open command)
        call this method, so they share the one flag.
        """
        if self._click_disabled_notice_shown:
            return False
        self._click_disabled_notice_shown = True
        return True

    def mark_grid_mouse_channel_suspect(self) -> None:
        """Record that a pointer send to Input ended without an answer.

        Calls ``MouseGridController.mark_grid_mouse_channel_suspect``. While
        the flag is set, ``grid_pointer_channel_quiescent`` sends a
        ``channel_probe`` to Input and reports the channel quiet only when
        the probe answers.
        """
        self.mouse_grid.mark_grid_mouse_channel_suspect()

    async def grid_pointer_channel_quiescent(self, *, trace_id: str) -> bool:
        """Wait until no grid pointer action can still act on the desktop.

        Calls ``MouseGridController.grid_pointer_channel_quiescent`` with the
        same argument and returns its result.
        """
        return await self.mouse_grid.grid_pointer_channel_quiescent(
            trace_id=trace_id,
        )

    def grid_session_in_transition(self) -> bool:
        """True while a grid open / next_screen is between fence and paint.

        Calls ``MouseGridController.grid_session_in_transition`` and returns
        its result.
        """
        return self.mouse_grid.grid_session_in_transition()

    def mark_screen_read_started(self) -> None:
        """Count a screen read in; calls ``_mark_screen_read_started``."""
        self._mark_screen_read_started()

    def mark_screen_read_finished(self) -> None:
        """Count a screen read out; calls ``_mark_screen_read_finished``."""
        self._mark_screen_read_finished()

    def put_grid_gui_event(self, payload: dict, trace_id: str) -> None:
        """Put one grid payload on the GUI state queue; never raises.

        Calls ``MouseGridController.put_grid_gui_event`` with the same
        arguments.
        """
        self.mouse_grid.put_grid_gui_event(payload, trace_id)

    def track_pending_pointer_task(self, task: "asyncio.Task[None]") -> None:
        """Record a pointer-capable background send for the session fence.

        Calls ``MouseGridController.track_pending_pointer_task`` with the
        same task.
        """
        self.mouse_grid.track_pending_pointer_task(task)

    def wheelhouse_process_ids(self) -> list[int]:
        """Process ids of every Wheelhouse process this process can name.

        Calls ``_wheelhouse_process_ids`` and returns its result.
        """
        return self._wheelhouse_process_ids()

    def speech_sentence_closed_event(self) -> "Optional[asyncio.Event]":
        """The speech processor's ``sentence_closed_event``, or None.

        Follows ``service_manager.speech_handler.speech_processor`` on each
        call; a missing link gives None. The value is not checked, so the
        caller checks that it is an ``asyncio.Event``.
        """
        processor = getattr(
            getattr(
                getattr(self, "service_manager", None),
                "speech_handler", None,
            ),
            "speech_processor", None,
        )
        return getattr(processor, "sentence_closed_event", None)

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
                # wh-dictation-paste-loss-load.1.9: the replay can take
                # about 3.5 s under the copy budget, so Logic waits up to
                # 10 s for the true answer. The request still expires
                # from the send queue after 5 s, so a replay that waited
                # that long for delivery never pastes into a window that
                # has changed since the click.
                # crewcut: when the request expires in the send queue,
                # app.py (about line 837) logs the drop through
                # _log_ipc_dropped and does not end this wait. So in an
                # Input stall of more than 5 s, the follow-up notice
                # shows at 10 s instead of 5 s; no words paste late (boss
                # ruling 12:32 on .1.9). Remove the limit by ending the
                # caller's wait on an expired drop; that changes the drop
                # path for every send_request caller.
                raw_response = await self.app.send_request(
                    ACTION_NAME, params,
                    timeout_s=10.0, delivery_ttl_s=5.0,
                )
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

    async def start_help_online(
        self,
        explained: bool = False,
        source: str = "menu",
        do_not_show_again: bool | None = None,
    ) -> None:
        """Decide what Help does: the explanation window, or the browser.

        Both ways in end here. The GUI process shows the Help menu entry but
        holds no settings, so it sends a command; the spoken command "help"
        calls this method through the logic controller. One method, so there
        is no second copy of the decision to keep in step.

        A new user who reaches the Gemini sign-in page with no explanation
        does not know what to do there, so an explanation window comes first.
        The window's Assistant button sends the same command back with
        ``explained`` true, which is what stops the window appearing again in
        answer to its own button. Setting ai.help.explain_before_open to
        false turns the window off. That command also carries the window's
        check box state as ``do_not_show_again``; this method, in the Logic
        process, saves the setting (only when the value changes) and then
        writes the marker file, and a failed write shows the user a notice.
        The GUI process writes nothing.

        The one exception to "false turns the window off": while the marker
        file is missing, a user whose setting is false sees the window once
        more, with the check box ticked, because the assistant moved to a
        Gemini Notebook that takes only a Google Account
        (wh-assistant-explainer-once-more).

        Nothing here is allowed to raise. This runs as a background task in
        the process that routes speech, and a browser that will not start is
        not a reason to take that down.
        """
        config = getattr(self, "config_service", None)
        if not config:
            logger.warning("Help: no settings available, cannot open help.")
            return

        if explained and do_not_show_again is not None:
            # Before the blank-address check: the window is modeless, so the
            # address can be blanked while it is open, and the choice made
            # in the window is still the user's to keep.
            notice = await _record_help_explainer_choice(
                config, getattr(self, "state_manager", None), do_not_show_again
            )
            if notice:
                self._send_gui_notification(notice)

        assistant_url = config.get("ai.help.assistant_url", "")
        if not assistant_url:
            # Blanking the setting is how a user turns online help off, so
            # this is a plain statement of fact rather than an error. The
            # explanation window stays shut: its one button would have
            # nothing to open. This check comes first on purpose: the
            # window is modeless, so the address can be blanked while it is
            # open, and the Assistant button must then say so rather than
            # open an empty page.
            logger.info(
                "Help: source=%s explained=%s, online help is not configured.",
                source,
                explained,
            )
            self._send_gui_notification(
                "Online help is not configured. Set assistant_url under [ai.help]."
            )
            return

        if not explained:
            explain = bool(config.get("ai.help.explain_before_open", True))
            once_more = False
            if not explain:
                # Setting false: the window is shown once more while the
                # marker is missing, with the box ticked to match the
                # user's earlier choice. load_hint_shown never raises, and
                # treats an unreadable marker as present.
                from services.wheelhouse.click_first_use_hint import (
                    load_hint_shown,
                )
                once_more = not await asyncio.to_thread(
                    load_hint_shown, default_help_explainer_marker_path()
                )
            if explain or once_more:
                logger.info(
                    "Help: source=%s explained=%s, asking for the explanation "
                    "window.",
                    source,
                    explained,
                )
                self._request_help_explainer(start_ticked=once_more)
                return

        logger.info(
            "Help: source=%s explained=%s, opening the browser.",
            source,
            explained,
        )
        try:
            import webbrowser
            from services.wheelhouse.utils.launch_cwd import (
                ensure_browser_start_folder,
            )
            # wh-installer-update-folder-locked: the browser starts outside
            # the app folder, so it cannot keep an update from deleting it.
            ensure_browser_start_folder()
            opened = await asyncio.to_thread(webbrowser.open, assistant_url)
            if not opened:
                # webbrowser.open reports the common failure by returning
                # False rather than raising: on Windows it catches the
                # OSError from os.startfile (wh-open-url-action.1.2). The
                # exception arm below would never see it, so a browser that
                # will not start would open nothing and say nothing. The
                # address stays out of the line: it is a user setting, and
                # the two browser launches in speech/actions.py redact what
                # they log for the same reason (wh-assistant-button-
                # explainer.1.1).
                logger.warning("Help: the browser reported failure opening help.")
                self._send_gui_notification(
                    "Wheelhouse could not open your browser."
                )
        except Exception as exc:
            logger.warning("Help: could not open the browser: %s", exc)
            self._send_gui_notification("Wheelhouse could not open your browser.")

    def _request_help_explainer(self, start_ticked: bool = False) -> None:
        """Ask the GUI process for the explanation window. Never raises.

        ``start_ticked`` asks for the check box to start ticked; it is set
        only for the once-more showing (wh-assistant-explainer-once-more).
        Without it the message carries no flag and the box starts clear.
        """
        state_manager = getattr(self, "state_manager", None)
        queue = getattr(state_manager, "state_to_gui_queue", None)
        if queue is None:
            logger.warning("Cannot show the help explanation, no GUI queue.")
            return
        message = {"action": "open_help_explainer"}
        if start_ticked:
            message["start_ticked"] = True
        try:
            queue.put_nowait(message)
        except Exception as exc:
            logger.warning("Help explanation could not be queued: %s", exc)

    def _send_gui_notification(self, message: str) -> None:
        """Put a one-line notice on the GUI queue. Never raises."""
        state_manager = getattr(self, "state_manager", None)
        queue = getattr(state_manager, "state_to_gui_queue", None)
        if queue is None:
            logger.warning("Cannot show a notice, no GUI queue: %s", message)
            return
        try:
            queue.put_nowait({
                "action": "show_notification",
                "title": "Wheelhouse",
                "message": message,
                "timeout": 5,
            })
        except Exception as exc:
            logger.warning("Notice could not be queued: %s", exc)

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
            "Wheelhouse couldn't try again. Say the words again, "
            "then click Try it anyway."
        )
        try:
            self.state_manager.state_to_gui_queue.put_nowait({
                "action": "show_notification",
                "title": "Wheelhouse",
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

    async def _send_te_control_command(self, action: str, params: dict) -> bool:
        """Send a terminal-editor control command with bounded retries.

        wh-overlay-slow-uia-stale-badges.14.19: a dropped
        terminal_editor_cancelled or _te_event_ack leaves the
        input-process proxy active with no editor on screen, so the
        next dictation is swallowed by a phantom session. Retry a
        bounded number of times; log an ERROR when every attempt fails
        so the wedge is visible in the log.

        wh-overlay-slow-uia-stale-badges.14.24: queue acceptance is not
        delivery (.14.10) -- send_command's True only said the payload
        entered Logic's outbound queue, and a post-acceptance drop
        (deadline expiry, framing failure, shutdown) lost the cleanup
        invisibly. The command now goes as an acknowledged send_request:
        the input-process handler puts an ok/error response, and a
        raised error (IpcDeliveryError, TimeoutError) or an error-dict
        response counts as a failed attempt. Both actions are in
        _DURABLE_COMMAND_ACTIONS, so send_request stamps the 120 s
        durable delivery budget. Duplicate deliveries across retries
        are safe: the .14.17 request_id fence makes the input-process
        handlers idempotent for a given session. The params dict is
        passed unchanged on every attempt.
        """
        delays = getattr(self, "_te_control_send_retry_delays", None)
        if not isinstance(delays, (tuple, list)):
            delays = _TE_CONTROL_SEND_RETRY_DELAYS
        attempts = 1 + len(delays)
        for attempt, delay in enumerate((0.0, *delays), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self.app.send_request(action, params)
            except Exception as e:
                logger.debug(
                    "%s: attempt %d/%d failed: %s",
                    action, attempt, attempts, e,
                )
                continue
            if isinstance(response, dict) and response.get("error"):
                logger.debug(
                    "%s: attempt %d/%d returned an error response: %s",
                    action, attempt, attempts, response.get("message", ""),
                )
                continue
            if attempt > 1:
                logger.info(
                    "%s: acknowledged on attempt %d/%d",
                    action, attempt, attempts,
                )
            return True
        logger.error(
            "%s: no acknowledgement on any of %d attempts; "
            "the input-process proxy may stay active until force_cleanup "
            "(params=%s)",
            action, attempts, params,
        )
        return False

    async def _handle_te_cancelled(self, request_id: str = ""):
        """GUI cancelled terminal editor. Notify Input to clear proxy state.

        wh-g2-refactor.18: the bridge into the focus-redirect path is
        gone with the path. Only the input-process proxy notification
        remains.

        wh-overlay-slow-uia-stale-badges.14.17: forwards the session's
        show request_id so the input-process proxy cleans up only the
        session that was cancelled. An empty rid still notifies Input
        (the proxy treats it as an unconditional recovery signal).
        """
        logger.debug("Terminal editor cancelled by GUI (rid=%s)", request_id)
        await self._send_te_control_command(
            "terminal_editor_cancelled", {"request_id": request_id},
        )

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
        await self._send_te_control_command(
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
    ) -> bool:
        """Send a retract+replay IPC and await the GUI response.

        See Section 2 of the G2 design doc. Returns True only when the
        GUI confirmed the retract-and-replay succeeded (a validated
        echo with an empty failure_reason); False on every other
        outcome -- timeout, echo mismatch, underrun, rebuild fence,
        declined. Never raises for those; the next STT update heals
        partial / dropped retracts. The caller may use the False return
        to refuse to advance state the GUI never installed
        (wh-whole-utterance-command-matching.1.1); the send itself is
        fire-and-heal either way.
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
            return False
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
                return False
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
            return False
        # Boundary check: the GUI MUST echo the request's chars_requested
        # value (round 1 / codex finding A, wh-g2-refactor.5.1).
        if response.get("chars_requested") != chars_requested:
            logger.warning(
                "retract_editor_text_response chars_requested mismatch "
                "(sent=%d got=%s rid=%s)",
                chars_requested, response.get("chars_requested"), request_id,
            )
            return False
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
            return False
        if failure_reason == "":
            return True
        if failure_reason == "ledger_underrun":
            logger.info(
                "retract_editor_text underrun: removed %d of %d (rid=%s); "
                "not replaying",
                response.get("chars_removed", 0), chars_requested, request_id,
            )
            return False
        if failure_reason in ("stale_generation", "editor_rebuilt"):
            logger.info(
                "retract_editor_text fenced by rebuild: %s (rid=%s)",
                failure_reason, request_id,
            )
            return False
        logger.info(
            "retract_editor_text declined: %s (rid=%s)",
            failure_reason, request_id,
        )
        return False

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

    def _get_lead_word_controller(self):
        """Lazily construct the 'Your training word' window controller.

        wh-first-sound-lost.23. Built on the first lw_* action, or on the
        first lead_word capability frame, so construction never races
        service startup. Window states go out on the state queue
        (lw_state messages); provider commands go out only to the active
        STT client (send_command_to_active_stt), never broadcast, because
        a superseded provider must not record the user's voice.
        """
        if self._lead_word_controller is not None:
            return self._lead_word_controller
        from speech.lead_word_controller import LeadWordController

        logic = self

        class _LeadWordProviderChannel:
            """Bridges the sync controller to the async STT WebSocket."""

            def send_command(self, command: str, request_id: str) -> bool:
                ws = getattr(logic.app, "websocket_manager", None)
                if not ws:
                    logger.error(
                        "Cannot send lead-word command: WebSocket manager not available"
                    )
                    return False
                logic.create_task_with_error_handling(
                    self._send_and_report(ws, command, request_id),
                    "LeadWordSendCommand",
                )
                return True

            async def _send_and_report(self, ws, command: str, request_id: str) -> None:
                # A raise here would shut the program down, so catch all.
                try:
                    sent = await ws.send_command_to_active_stt(
                        command, request_id=request_id,
                    )
                except Exception:
                    logger.exception("Lead-word command %s failed to send", command)
                    sent = False
                if not sent:
                    controller = logic._lead_word_controller
                    if controller is not None:
                        controller.on_send_failed(request_id)

            def hold_transcription(self, done) -> None:
                logic.create_task_with_error_handling(
                    self._hold_and_continue(done), "LeadWordHoldTranscription",
                )

            @staticmethod
            async def _hold_and_continue(done) -> None:
                # ``done`` runs even when the hold fails: the playback
                # hold has its own timer, and a window stuck on a sound
                # that never starts is worse than a sound heard by the
                # engine.
                try:
                    await logic.state_manager.hold_engine_for_lead_word_playback()
                except Exception:
                    logger.exception("Lead-word transcription hold failed")
                finally:
                    try:
                        done()
                    except Exception:
                        logger.exception("Lead-word playback start failed")

            def release_transcription(self) -> None:
                logic.state_manager.release_engine_after_lead_word_playback()

        self._lead_word_controller = LeadWordController(
            send_to_gui=self.state_manager.state_to_gui_queue.put_nowait,
            provider=_LeadWordProviderChannel(),
        )
        return self._lead_word_controller

    def _handle_lead_word_action(self, action: str, command: dict) -> None:
        """Route one lw_* GUI action to the lead-word controller.

        The controller guards its own transitions; this wrapper only
        protects the listener loop from a construction failure.
        """
        try:
            self._get_lead_word_controller().handle_gui_action(action, command)
        except Exception as e:
            logger.error(
                f"Lead-word action '{action}' failed: {e}", exc_info=True,
            )

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
                    "refreshed. Restart Wheelhouse or try saving again."
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
                    whole_utterance_only=(
                        data.get("whole_utterance_only") is True
                    ),
                    # wh-pattern-override-doc-id A2: a Customize save carries
                    # the built-in's durable name so the copy keeps overriding
                    # that built-in after a release rewrites its expression.
                    # Duplicate and Add send no doc_id and get none.
                    doc_id=data.get("doc_id"),
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
                # hint_engine: the engine's hint support the runtime
                # matcher refuses hint patterns by, so "boost" under a
                # non-hint engine answers "no command" here as well
                # (wh-boost-engine-qualification.1.1).
                result = run_test_phrase(
                    data["text"], parser.patterns, parser.matcher,
                    hint_engine=parser.hint_engine,
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
                    catalog=parser.pattern_catalog,
                    hint_engine=parser.hint_engine,
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
        #
        # The grant travels as an acknowledged REQUEST
        # (wh-overlay-slow-uia-stale-badges.14.14): send_command returns on
        # queue acceptance, and the sender can still drop the payload
        # afterward (expired, unread frame, event or write failure), so an
        # acceptance-based SUCCESS reset the click counter while the running
        # session never received the grant. SUCCESS now requires the Input
        # process's response; a timeout, an IpcDeliveryError, or an error
        # response is a failed attempt.
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
        last_failure = "no attempt ran"
        for attempt, delay in enumerate((0.0, *delays), start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self.app.send_request(
                    "add_soft_allow_tuple", payload
                )
            except Exception as exc:
                last_exc = exc
                last_failure = str(exc) or type(exc).__name__
                logger.warning(
                    "add_soft_allow: IPC send attempt %d/%d failed after "
                    "successful disk write: %s",
                    attempt, attempts, last_failure,
                )
                continue
            # Only an explicit error response counts as a failure, so test
            # doubles whose send_request returns a plain mock object keep
            # their success semantics (same narrowness rule as the .14.10
            # exact-False check this replaces).
            if isinstance(response, dict) and response.get("error"):
                last_failure = str(
                    response.get("message") or "input process reported an error"
                )
                logger.warning(
                    "add_soft_allow: IPC attempt %d/%d rejected by the "
                    "input process after successful disk write: %s",
                    attempt, attempts, last_failure,
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
            attempts, last_failure,
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

    async def _set_config_value_and_apply(
        self, key, value, request_id=None,
    ) -> bool:
        """Save one setting, then apply it to the running overlay (wh-numbers-stay-setting).

        Awaits ``StateManager.set_config_value`` and returns its result
        unchanged. When the save succeeded and ``key`` is one the overlay reads
        live, the running machine takes the new value, so the next click or
        window change follows it with no restart.
        """
        saved = await self.state_manager.set_config_value(
            key, value, request_id=request_id,
        )
        self.overlay.apply_saved_overlay_keep_numbers([key], saved)
        return saved

    async def _set_config_values_and_apply(
        self, values, request_id=None,
    ) -> bool:
        """Save several settings, then apply them to the running overlay.

        The group form of ``_set_config_value_and_apply``; returns the
        ``StateManager.set_config_values`` result unchanged.
        """
        saved = await self.state_manager.set_config_values(
            values, request_id=request_id,
        )
        self.overlay.apply_saved_overlay_keep_numbers(list(values or {}), saved)
        return saved


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
        # wh-first-sound-lost.23: the lw_* actions of the training-word window.
        lead_word_actions = {
            name: (lambda name=name: self._handle_lead_word_action(name, command))
            for name in _LEAD_WORD_GUI_ACTIONS
        }
        return {
            **lead_word_actions,
            "request_initial_state": self.state_manager.send_state_update,
            "toggle_speech_enabled_state": self.state_manager.toggle_speech_enabled_state,
            "ptt_start": lambda: self.state_manager.ptt_start(source=command.get("source", "unknown"), request_id=command.get("request_id")),
            "ptt_stop": lambda: self.state_manager.ptt_stop(reason=command.get("reason", "released")),
            "set_speech_interaction_mode": lambda: self.state_manager.set_speech_interaction_mode(command.get("mode", "toggle")),
            "toggle_button_visibility": self.state_manager.toggle_button_visibility,
            "toggle_log_level": self.toggle_log_level,
            "toggle_interim_results": self.toggle_interim_results,
            "restart_program": self.restart_program,
            "restart_stt_service": self.restart_stt_service,
            "set_config_value": lambda: self.create_task_with_error_handling(self._set_config_value_and_apply(command.get('key'), command.get('value'), request_id=command.get('request_id')), "SetConfigValue"),
            "set_config_values": lambda: self.create_task_with_error_handling(self._set_config_values_and_apply(command.get('values') or {}, request_id=command.get('request_id')), "SetConfigValues"),
            "get_config_values": lambda: self.create_task_with_error_handling(self.state_manager.get_config_values(command.get('keys') or [], command.get('request_id')), "GetConfigValues"),
            "switch_stt_provider": lambda: self.create_task_with_error_handling(self._switch_stt_provider(command.get('provider')), "SwitchSTTProvider"),
            "switch_ai_provider": lambda: self.create_task_with_error_handling(self._switch_ai_provider(command.get('provider')), "SwitchAIProvider"),
            # wh-parakeet-model-download-offer: Download now on the
            # Parakeet model offer. The handler asks the locator again,
            # starts the installer console, then shuts Wheelhouse down.
            "parakeet_download_now": lambda: self.create_task_with_error_handling(self._handle_parakeet_download_now(), "ParakeetDownloadNow"),
            # wh-release-update-check: Update now on the update notice.
            # The GUI sends the version and the release tag it was shown.
            # The handler is a coroutine: it downloads in a worker thread.
            "update_now": lambda: self.create_task_with_error_handling(self._handle_update_now(command.get("version"), command.get("tag")), "UpdateNow"),
            # wh-about-check-updates: the About Wheelhouse window's "Check
            # for updates" button, and the voice command through the GUI.
            # The starter refuses a second check while one runs.
            "check_for_updates": lambda: self._start_manual_update_check(),
            # wh-blocked-mic-message.1.2: the GUI process sends this when the
            # update notice becomes visible, with its version. The handler
            # writes the shown-notice record, so a notice that waits for a
            # place on the screen leaves no record behind.
            "update_notice_shown": lambda: self.create_task_with_error_handling(self._handle_update_notice_shown(command.get("version", "")), "UpdateNoticeShown"),
            # The strict "is True" is deliberate. This command crosses a
            # process boundary, so the field is whatever the sender put
            # there, and bool("false") is True. Only the literal boolean
            # skips the explanation window; anything else takes the full
            # decision (boss ruling on wh-assistant-button-explainer). The
            # check box state follows the same rule: only a real boolean is
            # a choice, and anything else records nothing
            # (wh-assistant-explainer-once-more).
            "open_help_online": lambda: self.create_task_with_error_handling(self.start_help_online(explained=command.get("explained") is True, source=str(command.get("source", "menu")), do_not_show_again=command.get("do_not_show_again") if isinstance(command.get("do_not_show_again"), bool) else None), "OpenHelpOnline"),
            # wh-first-run-notice.1.1: the GUI process sends this once the
            # first-run notice is on screen. The handler writes the marker,
            # so a notice nobody saw leaves no marker behind.
            "first_run_notice_shown": lambda: self.create_task_with_error_handling(self._handle_first_run_notice_shown(), "FirstRunNoticeShown"),
            "help_ask": lambda: self.create_task_with_error_handling(self._handle_help_ask(command.get("question", "")), "HelpAsk"),
            "help_reset": lambda: self._handle_help_reset(),
            "help_cancel": lambda: self._handle_help_cancel(),
            "te_cancelled": lambda: self.create_task_with_error_handling(
                self._handle_te_cancelled(command.get("request_id", "")),
                "TECancelled",
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
            # wh-jfavj / wh-g4oma: the GUI sends this message when the user
            # clicks a numbered overlay item (Phase 1.5 voice element-clicking).
            # NumberedOverlayController.handle_snapshot_item_clicked gets it.
            "snapshot_item_clicked": lambda: self.create_task_with_error_handling(
                self.overlay.handle_snapshot_item_clicked(command),
                "SnapshotItemClicked",
            ),
            # wh-n29v.67: GUI emits this after it applies / fails / clears a
            # numbered-overlay paint. The handler validates the payload, gates on
            # the overlay being active, maps the wire state to a PAINT_ACK
            # OverlayEvent carrying the wire generation pair, and applies it
            # through the overlay's event handler so the held
            # overlay state machine is driven (the machine's own generation
            # gate drops a stale ack).
            "overlay_state_changed": lambda: self.create_task_with_error_handling(
                self.overlay.handle_overlay_state_changed(command),
                "OverlayStateChanged",
            ),
            # wh-overlay-rewalk-after-filter: the Pattern Manager dialog emits
            # this when its tree changes what a UIA walk would return (the
            # filter box hiding rows, or a repopulate). The window itself
            # announces nothing a Logic-side hook can see, so this message IS
            # the signal. The handler validates the payload and feeds the
            # existing focus-change re-walk path. The action name carries NO
            # "pm_" prefix on purpose -- the listener above routes every pm_*
            # action to _handle_pattern_manager_action before this table.
            "pattern_manager_tree_changed": lambda: (
                self._handle_pattern_manager_tree_changed(command)
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

    async def listen_for_gui_commands(self, commands_from_gui_queue: Queue):
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

        # wh-overlay-refactor-pilot.2: stop the overlay focus hooks, cancel every
        # overlay timer, and drop the retained snapshot summaries. getattr guard
        # for the bypassed-__init__ case.
        overlay = getattr(self, "overlay", None)
        if overlay is not None:
            overlay.shutdown()

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
                "voice-clicking disabled); any stale Wheelhouse-owned flag "
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
            self.overlay.start_overlay_focus_hooks()

            # wh-overlay-slow-uia-stale-badges.9: tell the GUI a fresh Logic
            # is here -- drop leftover badges, reset the generation gate,
            # cancel the badge lease. Unconditional (a previous Logic may
            # have painted even if this config disables the overlay);
            # best-effort like every GUI enqueue.
            self.overlay.send_overlay_startup_reset()

            # The one read of stt.mode this program still makes, and it
            # reports rather than decides: remote is the only mode since
            # wh-in-process-capture-removal, so nothing branches on the
            # value. A settings file left over from the in-process build
            # has to be told that here, once, or it starts remote without
            # a word about the setting it lost.
            warn_if_stt_mode_unsupported(self.config_service)

            # Start the app's internal components (IPC tasks, WebSocket)
            await self.app.start(
                self.config_service.get("SPEECH_WEBSOCKET_HOST", "localhost"),
                0,  # OS assigns free port; actual port read back from server socket
                self.handle_transcribed_text,
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
            if self.service_manager.remote_stt_launcher:
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

            # The pause for an app that holds the speaker in exclusive mode
            # applies unless the setting is false, whatever Windows said about
            # the microphone (wh-kodi-playback-echo-stutter). "auto" and true
            # both leave it on. An unreadable value logs one warning here and
            # one in decide_audio_suppression below, and counts as "auto".
            #
            # This runs BEFORE the STT provider starts, and so does the
            # detector (wh-kodi-playback-echo-stutter.1.1): a provider that
            # connects while no exclusive state is known could have its speech
            # acted on while an app holds the speaker. With the pause on, wait
            # for the detector's first answer, bounded by
            # FIRST_ANSWER_TIMEOUT_SECONDS. With the pause off there is
            # nothing to learn, so nothing waits. start_services starts the
            # detector again; start() does nothing when it already runs.
            # crewcut: the first answer costs up to the timeout at startup
            # when the detector hangs. To remove the limit, start the provider
            # while the detector runs and refuse the provider's text in
            # StateManager until the first answer arrives.
            exclusive_speaker_pause_on = (
                parse_audio_suppression_setting(
                    self.config_service.get(
                        AUDIO_SUPPRESSION_SETTING, AUDIO_SUPPRESSION_DEFAULT
                    )
                )
                is not False
            )
            self.state_manager.apply_exclusive_speaker_pause_gate(
                exclusive_speaker_pause_on
            )
            exclusive_speaker_monitor = self.service_manager.exclusive_speaker_monitor
            if exclusive_speaker_monitor:
                exclusive_speaker_monitor.start()
                if exclusive_speaker_pause_on:
                    await exclusive_speaker_monitor.wait_first_answer()

            # Start STT
            if self.service_manager.remote_stt_launcher:
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

                _show_working_via_queue, _hide_working_via_queue = (
                    _working_dialog_callbacks(gui_queue)
                )
                self.service_manager.remote_stt_launcher.set_working_callback(
                    show=_show_working_via_queue,
                    hide=_hide_working_via_queue,
                )

                def _mark_stt_stopped(
                    provider_name: str,
                    *,
                    may_wait: bool = False,
                    generation: int | None = None,
                ) -> None:
                    # Runs on the launcher's startup-monitor thread. It
                    # only records the state; the three-second
                    # periodic_state_updater carries the correction to
                    # the GUI, so no state dict is built off the event
                    # loop (wh-remote-stt-robustness). generation names
                    # the launch the report is about, so a report from a
                    # launch that has already been replaced cannot clear
                    # or overwrite the record of the launch that
                    # replaced it (wh-launch-generation).
                    _reconcile_stopped_remote_provider(
                        self.state_manager,
                        self.service_manager.remote_stt_launcher,
                        provider_name,
                        may_wait=may_wait,
                        generation=generation,
                    )

                self.service_manager.remote_stt_launcher.set_provider_stopped_callback(
                    _mark_stt_stopped
                )
                # The first-run notice waits for this (wh-first-run-notice.2).
                self.service_manager.remote_stt_launcher.set_provider_ready_callback(
                    self._speech_provider_ready_callback()
                )
                # Wire up reverse reference so WebSocket manager can signal provider ready
                self.app.websocket_manager.remote_stt_launcher = self.service_manager.remote_stt_launcher
                # Auto-start the last selected remote STT provider
                self.service_manager.start_remote_stt()
                logger.info("Remote STT provider auto-start initiated")

            # Ask Windows, once, whether sound this computer plays has to
            # pause listening (wh-audio-suppression-auto). This has to happen
            # before the services start: the audio monitor publishes its first
            # answer as soon as it runs, and that answer is judged by this
            # decision.
            audio_suppression_active = await decide_audio_suppression(
                self.config_service.get(
                    AUDIO_SUPPRESSION_SETTING, AUDIO_SUPPRESSION_DEFAULT
                )
            )
            self.state_manager.apply_audio_suppression_decision(
                audio_suppression_active
            )

            # Consolidate all essential background tasks
            service_tasks = self.service_manager.start_services()

            # The GUI command listener is a critical background task.
            # We create it here and add it to our list of tasks to monitor.
            gui_listener_task = self.create_task_with_error_handling(
                self.listen_for_gui_commands(commands_from_gui_queue), 
                "GuiCommandListener"
            )

            for name, coro in self._startup_task_coroutines().items():
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

    # High process class keeps Logic scheduled under a saturated CPU; the
    # Below Normal class a Task Scheduler launch hands down starves it
    # (wh-process-priority-durable). After setup_logging so a refusal's
    # warning reaches the process log instead of bare stderr
    # (wh-process-priority-durable.1.4).
    from services.wheelhouse.utils.process_priority import (
        elevate_process_priority,
    )
    elevate_process_priority()

    # So the first paste starts no child process for pyperclip's OS
    # detection (wh-input-first-paste-stall).
    from services.wheelhouse.utils.clipboard_manager import (
        select_windows_clipboard,
    )
    select_windows_clipboard()

    # Display startup banner with version info
    logger.info(f"{get_startup_banner('Wheelhouse')} - Starting")

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
