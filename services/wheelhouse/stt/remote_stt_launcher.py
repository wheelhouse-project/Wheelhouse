"""Remote STT provider discovery and lifecycle management.

This module discovers, starts, stops, and monitors remote STT providers.
Providers are discovered by scanning the services/stt_providers/ directory for config.toml
files containing a [provider] section.

Key Features:
  - Provider discovery via config.toml [provider] sections
  - Start providers by launching their launcher.py subprocess
  - Stop providers by sending shutdown command via WebSocket
  - Check provider status via PID files
"""
import logging
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import psutil

try:
    import tomllib
except ImportError:
    import tomli as tomllib

if TYPE_CHECKING:
    from integrations.websocket_manager import WebSocketManager
    from typing import Callable

logger = logging.getLogger(__name__)

# Type alias for notification callback
NotifyCallback = "Callable[[str, str], None]"

# How many launches keep a signal slot. A monitor outlives the switch
# away from its own provider, so a slot cannot be dropped when the next
# launch starts; a small ring covers that, because a slot is read by
# the monitor that owns it and by the late-death watch that monitor
# hands its child to (wh-launch-generation.1.1,
# wh-provider-late-death-watch).
#
# An earlier version of this comment said the watch needs no bigger
# ring because it holds its slot object directly. Holding the object is
# true; the conclusion drawn from it was wrong. `_launch_signal` cannot
# tell an evicted generation from one it has never seen, so a ready or
# a failure arriving for an evicted launch creates a FRESH slot and
# writes only that one. The owner still waiting on the old object never
# sees the signal, and the fresh slot's `closed` is False, so
# `signal_provider_startup_failed` skips the orphan report a closed
# slot exists to trigger. Measured directly against this code, not
# inferred. Reaching it takes eight later launches inside one owner's
# wait, and the late-death watch can wait 300 seconds
# (wh-provider-late-death-watch.2.5).
_MAX_TRACKED_LAUNCH_SIGNALS = 8


class _LaunchSignal:
    """How one launch reports the end of its own startup.

    One per launch, so no launch can wake, silence, or clear another's.

    `closed` records that the owning monitor has finished reading this
    slot and will not look again. `reported` records that someone has
    already told the rest of the program that this launch is not
    running. Both are written under the launcher's signal lock, because
    the monitor thread and the event-loop thread race for them: a
    failure landing at the monitor's deadline was read as "not ready"
    and then dropped (wh-launch-generation.2.2).

    `owners` counts the parties HOLDING this object right now. Every
    reader takes the slot once and keeps it -- the startup monitor for
    its whole bounded wait, the late-death watch for
    `_LATE_DEATH_WATCH_LIMIT` after that -- so dropping a held slot from
    the ring sends the launch's own outcome to a different object and
    leaves the holder waiting out its timeout on a slot nothing will
    ever set (wh-launch-signal-eviction).
    """

    __slots__ = (
        "event", "failed", "closed", "reported", "owners",
        "ready", "watchdog_retry_used", "stop_requested",
        "watchdog_report_pending",
    )

    def __init__(self) -> None:
        self.event = threading.Event()
        self.failed = False
        self.closed = False
        self.reported = False
        self.owners = 0
        self.ready = False
        self.watchdog_retry_used = False
        self.watchdog_report_pending = False
        self.stop_requested = False

# Default timeout for provider startup (seconds). GPU/Vulkan providers can
# take 60s+ on first load (kernel compilation, CUDA context init, model paging).
# Individual providers can override via [provider].startup_timeout_seconds.
DEFAULT_STARTUP_TIMEOUT = 90

# How often the late-death watch asks a pre-ready child whether it is still
# alive, and how long it goes on asking. The watch outlives the startup
# monitor, so it is the only party that can notice a child exiting after the
# bounded wait has ended (wh-provider-late-death-watch).
_LATE_DEATH_POLL_INTERVAL = 1.0
_LATE_DEATH_WATCH_LIMIT = 300.0


class RemoteSTTLauncher:
    """Discovers and manages remote STT provider lifecycle.

    Attributes:
        services_dir: Path to the services directory containing providers.
        app_data_dir: Path to app data directory for PID files.
        ws_host: WebSocket host to pass to providers.
        ws_port: WebSocket port to pass to providers.
        _providers: Cached list of discovered providers.
        _ws_manager: WebSocket manager for sending commands to providers.
    """

    def __init__(
        self,
        services_dir: Optional[Path] = None,
        app_data_dir: Optional[Path] = None,
        ws_host: str = "localhost",
        ws_port: int = 0,
        wake_word_config: Optional[dict] = None,
        google_credentials_file: str = "",
    ):
        """Initialize RemoteSTTLauncher.

        Args:
            services_dir: Path to services directory. Defaults to project's services/.
            app_data_dir: Path to app data directory. Defaults to %APPDATA%/WheelHouse.
            ws_host: WebSocket host to pass to providers when starting.
            ws_port: WebSocket port to pass to providers when starting.
            wake_word_config: Wake word configuration dict with keys: enabled, keyword,
                sensitivity, mode, model_dir. Passed to STT providers as CLI args.
            google_credentials_file: Path to the Google service-account key file
                (stt.google.credentials_file). Passed to the google_stt provider
                as a --credentials-file CLI arg; empty means the provider uses
                the GOOGLE_APPLICATION_CREDENTIALS environment variable.
        """
        if services_dir is None:
            # Default: project_root/services/stt_providers/
            project_root = Path(__file__).parent.parent.parent.parent
            services_dir = project_root / "services" / "stt_providers"
        self.services_dir = Path(services_dir)

        if app_data_dir is None:
            # Default: %APPDATA%/WheelHouse (Windows) or ~/.wheelhouse (Linux)
            if sys.platform == "win32":
                app_data_dir = Path(os.environ.get("APPDATA", "")) / "WheelHouse"
            else:
                app_data_dir = Path.home() / ".wheelhouse"
        self.app_data_dir = Path(app_data_dir)
        self.app_data_dir.mkdir(parents=True, exist_ok=True)

        self.ws_host = ws_host
        self.ws_port = ws_port
        self.wake_word_config: dict = wake_word_config or {}
        self.google_credentials_file: str = google_credentials_file
        self._providers: Optional[list[dict]] = None
        self._ws_manager: Optional["WebSocketManager"] = None
        self._notify_callback: Optional["NotifyCallback"] = None
        self._show_working_callback: Optional["Callable[[str, Optional[int]], None]"] = None
        self._hide_working_callback: Optional["Callable[[Optional[int]], None]"] = None
        self._provider_stopped_callback: Optional["Callable[..., None]"] = None
        # Event signaled when provider sends "ready" notification via WebSocket
        # Initialized as set so is_starting returns False before any provider launch
        self._provider_ready_event: threading.Event = threading.Event()
        self._provider_ready_event.set()
        # True when the provider reported its startup FAILED
        # (kind="startup_failed"). The same event is set so is_starting
        # ends and _monitor_startup wakes; the flag tells them apart
        # (wh-google-creds-file-picker.1.5).
        self._provider_startup_failed: bool = False
        # One signal slot per launch. The flag and the event above are
        # ONE pair shared by every provider, so every launch's monitor
        # woke on every other launch's signal and had to decide whose it
        # was -- and whichever monitor looked first could clear a signal
        # that belonged to another launch, leaving its owner with
        # nothing to read. A monitor now waits on the slot for its own
        # launch and cannot see, clear, or consume any other
        # (wh-launch-generation.1.1).
        self._launch_signals: "OrderedDict[int, _LaunchSignal]" = OrderedDict()
        # Launches whose slot was dropped by the cap while it was still
        # closed and unclaimed. That pair is the handoff telling a late
        # failure nobody is left to report the launch
        # (wh-launch-generation.2.2); dropping the slot would cancel the
        # handoff without a word, so the debt outlives the slot
        # (wh-launch-signal-eviction).
        self._evicted_unreported: "OrderedDict[int, None]" = OrderedDict()
        # The monitor threads and the event loop thread all reach the
        # slots above. Without this lock a failure could land between a
        # monitor's read and its removal of its own slot, and be lost
        # (wh-launch-generation.2.2).
        self._launch_signals_lock: threading.RLock = threading.RLock()
        # Launches for which a connection reported a failed startup
        # without ever saying which provider it was. The websocket
        # manager cannot attribute such a failure to a launch, so it
        # drops it; this is where it says the failure happened, so the
        # launch that was starting at the time can consult it rather
        # than read the provider's silence as a slow cold start
        # (wh-launch-generation.2.3).
        self._undeclared_startup_failures: "set[int]" = set()
        # Threads carrying an orphaned launch's stopped report. A
        # failure whose monitor has already finished is reported from
        # the event loop thread, which must never block, and the report
        # has to be allowed to wait (wh-launch-generation.2.5). Keeping
        # the handles is what lets a test join them.
        self._orphan_report_threads: "list[threading.Thread]" = []
        # Threads watching a pre-ready child that outlived its startup
        # monitor. Keeping the handles is what lets a test join them, the
        # same reason the orphan reports above keep theirs
        # (wh-provider-late-death-watch).
        self._late_death_watch_threads: "list[threading.Thread]" = []
        # Instance state rather than module constants read directly, so a
        # test drives the watch without waiting on real time.
        self._late_death_poll_interval: float = _LATE_DEATH_POLL_INTERVAL
        self._late_death_watch_limit: float = _LATE_DEATH_WATCH_LIMIT
        # A launch generation number identifies one start of a provider.
        # A provider NAME cannot: nothing in it tells a launch that
        # failed from a later launch of the same provider, and the
        # engine record is written by name at three places
        # (wh-launch-generation, ruling on wh-remote-stt-robustness.2.8).
        self._launch_generation_counter: int = 0
        self._launch_generations: dict[str, int] = {}
        self._current_launch_generation: Optional[int] = None
        self._watchdog_shutdown = False
        # Popen handles for spawned provider subprocesses, keyed by provider name.
        # Used by _monitor_startup (wh-v0q) to check liveness on ready-timeout and
        # suppress false-failure notifications when the subprocess is still alive
        # (slow cold-start path).
        self._subprocesses: dict[str, subprocess.Popen] = {}

    def get_providers(self) -> list[dict]:
        """Get cached list of providers, discovering if not yet cached.

        Unlike discover_providers(), this returns cached results and only
        scans the disk on first call or after invalidate_cache().

        Returns:
            List of provider info dicts.
        """
        if self._providers is None:
            self.discover_providers()
        return self._providers or []

    def invalidate_cache(self) -> None:
        """Clear the provider cache, forcing re-discovery on next get_providers()."""
        self._providers = None

    def set_websocket_manager(self, ws_manager: "WebSocketManager") -> None:
        """Set the WebSocket manager for sending commands to providers.

        Args:
            ws_manager: WebSocket manager instance.
        """
        self._ws_manager = ws_manager

    def set_notify_callback(self, callback: "NotifyCallback") -> None:
        """Set callback for sending user notifications.

        Args:
            callback: Function taking (title, message) to send notifications.
        """
        self._notify_callback = callback

    def _notify(
        self, title: str, message: str, generation: Optional[int] = None,
        owner: Optional[str] = None,
    ) -> None:
        """Send a notification if callback is set.

        `generation` names the launch the notice belongs to, and the
        launch is read again here rather than trusted from the caller.
        No caller's answer is good enough by itself: the two monitor
        paths ask `launch_is_current` once and then do two things with
        the one answer, and `start_provider`'s failure path does not
        ask at all. A replacement stamped before delivery therefore
        leaves this call about to announce a failure for a provider
        that is starting normally (wh-launch-generation.2.9). Reading
        again immediately before the callback leaves a window of a few
        instructions instead of one spanning the dismiss that runs
        first (wh-launch-addressed-notices).

        A notice that names no launch is delivered, which is what every
        caller outside the launch paths does today.

        Args:
            title: Notification title.
            message: Notification message.
            generation: The launch this notice belongs to, or None.
            owner: The provider that launch belongs to, for a per-provider notice.
        """
        if self._notify_callback:
            selected = (self._launch_generations.get(owner) == generation if owner
                        else self.launch_is_current(generation))
            if not selected:
                # The current launch is read a second time for this
                # line alone; the drop above is the decision.
                logger.info(
                    f"Dropped the notice for launch generation "
                    f"{generation} ({title}: {message}): launch "
                    f"{self._current_launch_generation} is current now"
                )
                return
            try:
                self._notify_callback(title, message)
            except Exception as e:
                logger.warning(f"Failed to send notification: {e}")

    def set_working_callback(
        self,
        show: "Callable[[str, Optional[int]], None]",
        hide: "Callable[[Optional[int]], None]",
    ) -> None:
        """Set callbacks for showing/hiding the working dialog.

        Both take the launch generation the message belongs to, because
        one dialog is shared by every provider and a dismiss has to say
        which launch it is for. Whoever receives these decides what to
        do with an unwanted dismiss; this class only names the launch
        (wh-launch-addressed-notices).

        Args:
            show: Function taking a message string and the launch
                generation to show the working dialog.
            hide: Function taking the launch generation to hide it.
        """
        self._show_working_callback = show
        self._hide_working_callback = hide

    def _show_working(self, message: str, generation: Optional[int]) -> None:
        """Show working dialog if callback is set."""
        if self._show_working_callback:
            try:
                self._show_working_callback(message, generation)
            except Exception as e:
                logger.warning(f"Failed to show working dialog: {e}")

    def _hide_working(self, generation: Optional[int]) -> None:
        """Hide working dialog if callback is set."""
        if self._hide_working_callback:
            try:
                self._hide_working_callback(generation)
            except Exception as e:
                logger.warning(f"Failed to hide working dialog: {e}")

    def set_provider_stopped_callback(self, callback: "Callable[..., None]") -> None:
        """Set the callback for a provider that is known not to be running.

        Args:
            callback: Function taking the provider name and the may_wait
                and generation keywords. may_wait is True from a thread
                that has no work left after the call, so the callback
                may wait there -- the one in main.py waits for a
                replaced engine to exit before it decides nothing is
                running. THREE callers pass it: the startup-monitor
                thread, the short-lived thread
                _report_stopped_off_loop starts for an orphaned launch
                (wh-launch-generation.2.5), and the late-death watch the
                monitor's slow-start branch leaves running
                (wh-provider-late-death-watch). Naming only the monitor
                here was wrong once the second caller existed
                (wh-launch-generation.2.11), so this count is kept right
                as callers are added. It is False from
                an ordinary start_provider exception handler, which reports on
                whatever thread called it, and a switch calls
                start_provider on the event loop. Watchdog failures instead
                use _report_stopped_off_loop for survivor reconciliation.
                generation names the
                launch the report is about, so the engine record can
                refuse a report from a launch that has already been
                replaced (wh-launch-generation). The callback must not
                build a state dictionary or touch the event loop
                (wh-remote-stt-robustness.2.5, .2.6).
        """
        self._provider_stopped_callback = callback

    def _provider_stopped(
        self,
        provider_name: str,
        *,
        may_wait: bool = False,
        generation: Optional[int] = None,
    ) -> None:
        """Report that a provider is not running, if a callback is set.

        A failed start is learned here and nowhere else, and until this
        existed nothing told the rest of the program, so the tray kept
        showing the provider as the running one
        (wh-remote-stt-robustness).

        Args:
            provider_name: The provider that is not running.
            may_wait: True only from a thread that has no work left
                after the call: the startup-monitor thread, the
                short-lived thread _report_stopped_off_loop starts for
                an orphaned launch, and the late-death watch that the
                monitor's slow-start branch leaves running
                (wh-provider-late-death-watch). The watchdog also uses that
                off-loop reporter. An ordinary start_provider exception
                handler reports on whatever thread called it, and a
                switch calls start_provider on the event loop, so that
                report must never wait (wh-remote-stt-robustness.2.6).
            generation: The launch this report is about. The engine
                record compares it, so a report from a launch that has
                already been replaced cannot clear the record of the
                launch that replaced it (wh-launch-generation).
        """
        if self._provider_stopped_callback:
            try:
                self._provider_stopped_callback(
                    provider_name, may_wait=may_wait, generation=generation
                )
            except Exception as e:
                logger.warning(f"Failed to report a stopped provider: {e}")

    def _report_stopped_off_loop(
        self, provider_name: str, generation: Optional[int]
    ) -> bool:
        """Report a stopped provider from a thread of its own.

        The failure of a launch whose monitor has already finished is
        learned on the event loop thread, and that thread must never
        block (wh-remote-stt-robustness.2.6). Reporting there without
        waiting skips the survivor reconciliation in main.py, which is
        the whole reason the record is not simply cleared: the engine
        the switch replaced can still be transcribing, and clearing the
        record then leaves the tray showing nothing running
        (wh-remote-stt-robustness.2.5, wh-launch-generation.2.5).

        So the report is handed to a short-lived thread that has no work
        after the call, which is the condition may_wait names.

        Answers whether that thread started. Every caller spends
        something irrevocable before calling -- `signal.reported` for a
        slot still in the ring, the eviction debt for one the cap has
        dropped -- so a start that raises would lose the only report
        that launch will ever get, and leave the failed provider
        recorded as running. A False answer means the caller must put
        back what it spent, which `_restore_unspent_report` does
        (wh-launch-signal-eviction.2.1). No refusal of the handoff
        escapes as an exception, so the caller always reaches that
        answer and its own remaining work
        (wh-launch-signal-eviction.2.3).
        """
        thread = None
        tracked = False
        try:
            thread = threading.Thread(
                target=self._provider_stopped,
                args=(provider_name,),
                kwargs={"may_wait": True, "generation": generation},
                daemon=True,
            )
            with self._launch_signals_lock:
                # Finished threads are dropped here rather than joined:
                # nothing waits on them outside the tests, and a launcher
                # that ran for hours must not accumulate handles.
                self._orphan_report_threads = [
                    existing
                    for existing in self._orphan_report_threads
                    if existing.is_alive()
                ]
                self._orphan_report_threads.append(thread)
                tracked = True
            thread.start()
        except (RuntimeError, MemoryError) as e:
            # Caught narrowly and not re-raised, unlike the late-death
            # watch's start (wh-launch-signal-eviction.1.1), which gives
            # its hold back and lets the exception carry on. The
            # difference is who is left holding the problem: there the
            # monitor's own `finally` still has work to do, while here
            # the caller has already spent the launch's one report and
            # has more of its own work after this line.
            #
            # The whole handoff is guarded, not just `start()`. Three
            # regions run between the caller's spend and the running
            # thread, and all three can refuse. They are read from the
            # interpreter that runs this code -- CPython 3.12.10,
            # Lib/threading.py:
            #
            #   * `Thread.__init__` builds an `Event` (line 935), calls
            #     `_make_invoke_excepthook()` and adds the object to
            #     `_dangling`, so it raises MemoryError under
            #     exhaustion; it also raises RuntimeError outright when
            #     daemon threads are disabled in a subinterpreter, and
            #     this reporter asks for `daemon=True`.
            #   * The prune above rebuilds a list and calls `is_alive()`
            #     on every handle it keeps, which allocates.
            #   * `Thread.start` runs `_limbo[self] = self` and then
            #     `_start_new_thread(...)`, the only statements ahead of
            #     the new thread. The allocations either makes raise
            #     MemoryError, and `_start_new_thread` raises
            #     RuntimeError when the process cannot create another
            #     native thread. It re-raises after taking itself back
            #     out of `_limbo`, so no cleanup of its state is owed.
            #
            # The first guard covered only the third
            # (wh-launch-signal-eviction.2.2). The other two escaped it
            # and lost the report exactly as an unguarded start did; at
            # the declared-failure caller the escape also abandoned the
            # rest of its orphan loop and the startup-state update after
            # it (wh-launch-signal-eviction.2.3).
            #
            # Anything outside this set still propagates, including
            # KeyboardInterrupt, which must propagate: it can arrive
            # while the started thread is already running
            # (wh-launch-signal-eviction.2.2).
            #
            # One window stays open, deliberately, because it is much
            # narrower than the escape it replaces. The
            # `self._started.wait()` that ends `Thread.start` allocates
            # a lock of its own AFTER the thread is running, so a
            # MemoryError raised there is caught here and puts back a
            # report the live thread will also make. Reaching it needs
            # memory exhaustion inside those two small allocations AND a
            # later failure signal for the same launch to spend the
            # restored one-shot, while the escape it replaces needs only
            # the exhaustion. The costs are not equal either: a
            # duplicate report re-runs a callback that compares the
            # generation, and a lost one leaves a dead provider shown as
            # running until the launcher restarts.
            if tracked:
                with self._launch_signals_lock:
                    # Taken out in place rather than by rebuilding the
                    # list: this handler runs when the allocator may
                    # have just refused, so it asks for as little as it
                    # can. `Thread` defines no `__eq__`, so `remove`
                    # compares by identity. `tracked` is what says a
                    # handle was ever recorded -- a refusal during
                    # construction or the prune leaves nothing to take
                    # out.
                    self._orphan_report_threads.remove(thread)
            try:
                logger.error(
                    f"Could not start the thread that reports "
                    f"{provider_name} stopped (launch generation "
                    f"{generation}): {e}. The report is left owed rather "
                    f"than spent."
                )
            except MemoryError:
                # Building the message allocates, and it runs after the
                # refusal that brought us here. Losing the log line is
                # acceptable; losing the False answer is not, because
                # the caller's one report goes with it.
                pass
            return False
        return True

    def _restore_unspent_report(self, generation: Optional[int]) -> None:
        """Put back the one report a failed reporter start did not make.

        Which one-shot was spent depends on whether the slot is still in
        the ring, and the answer can change between the spend and this
        call: a launch landing in that window can evict the slot, and
        the eviction leaves no debt behind because `reported` was set to
        True on the way past. So this reads the ring rather than
        trusting the caller, and covers both cases
        (wh-launch-signal-eviction.2.1).
        """
        if generation is None:
            return
        with self._launch_signals_lock:
            signal = self._launch_signals.get(generation)
            if signal is not None:
                signal.reported = False
                return
            # The debt was removed by the claim a moment ago, so putting
            # it back cannot exceed the cap and cannot drop an older
            # debt. It goes back as the youngest entry, which is what it
            # is: the report is owed again as of now.
            self._evicted_unreported[generation] = None

    def _resolve_display_name(self, display_name: str, service_dir: Path) -> str:
        """Resolve {mode} placeholder in display_name using provider config.

        Reads the provider's config.toml to determine CPU/GPU mode.
        """
        try:
            config_path = service_dir / "config.toml"
            if config_path.exists():
                import tomllib
                with open(config_path, "rb") as f:
                    config = tomllib.load(f)
                use_gpu = config.get("model", {}).get("use_gpu", False)
                return display_name.replace("{mode}", "GPU" if use_gpu else "CPU")
        except Exception as e:
            logger.warning(f"Failed to resolve display name placeholder: {e}")
        return display_name.replace("{mode}", "CPU")

    def signal_provider_ready(self, generation: Optional[int] = None) -> None:
        """Signal that the provider has sent its 'ready' notification.

        Called by WebSocketManager when it receives a notification with
        'ready' or 'service ready' in the message. This cancels the
        startup timeout monitor.

        Args:
            generation: The launch the sending connection belongs to,
                the same stamp `signal_provider_startup_failed` reads.
                The provider being replaced keeps an active connection
                until the replacement connects, so its queued ready
                reaches this method while a DIFFERENT launch is
                starting. Carrying the sender's own generation is what
                sends the ready to that launch's own signal slot rather
                than to the replacement's, and what stops it from
                ending the replacement's starting state
                (wh-ready-connection-stamp, from QUESTIONS-2026-08-29
                item 24 answer (a)). None when the sender could not be
                identified, which keeps the behaviour this method had
                before it carried a generation at all.
        """
        if generation is None:
            generation = self._current_launch_generation
        # Both writes under ONE acquisition. Read apart, they are a
        # check-then-act of the same shape as
        # wh-launch-generation.2.14: a switch landing between the slot
        # write and the comparison below would set the replaced
        # launch's slot and then measure against the replacement.
        # Everything held across is non-blocking -- a dict lookup, a
        # `threading.Event` write, and one integer comparison -- which
        # is the condition `_end_starting_state` names for taking this
        # lock at all. The lock is an RLock and both callees acquire
        # it, so re-entering is safe.
        with self._launch_signals_lock:
            signal = self._launch_signal(generation)
            if signal is not None:
                signal.ready = not signal.failed
                signal.event.set()
            # `is_starting` is one flag shared by every provider, so a
            # ready from a launch that has already been replaced must
            # not end the replacement's startup. Setting it here with
            # no comparison was the whole defect: the replacement was
            # shown as started while dictation still came from the
            # provider it replaced (wh-ready-connection-stamp).
            self._end_starting_state(generation)
        logger.debug(
            f"Provider ready signal received (launch generation {generation})"
        )

    def signal_provider_startup_failed(
        self, generation: Optional[int] = None
    ) -> None:
        """Signal that the provider reported its startup FAILED
        (a notification with kind="startup_failed").

        Ends the starting state -- without this, a failed startup left
        is_starting true for the rest of the session and the startup
        suppression swallowed every later notification from the
        provider (wh-google-creds-file-picker.1.5).

        Args:
            generation: The launch the sending connection belongs to,
                recorded by WebSocketManager.add_client when that
                connection became the active stream. The provider being
                replaced keeps an active connection until the
                replacement connects, so its failure reaches this method
                while a DIFFERENT launch is starting; carrying the
                sender's own generation is what sends this failure to
                that launch's own signal slot rather than to the
                replacement's (wh-launch-generation). None when the
                sender could not be identified, which keeps the earlier
                behaviour of treating the failure as the current one's.
        """
        if generation is None:
            generation = self._current_launch_generation
        if generation is None:
            # No launch was ever stamped, so there is nothing to tell
            # this launcher's launches apart. Marking every one of them
            # keeps the behaviour an unstamped failure had before the
            # generation existed.
            targets = sorted(set(self._launch_generations.values()))
        else:
            targets = [generation]
        # A launch whose monitor has already finished has nobody left
        # to read its slot, so this thread reports the stop itself
        # rather than letting the failure go unrecorded
        # (wh-launch-generation.2.2). The reports are made after the
        # lock is released: _provider_stopped calls out to a callback.
        orphaned: "list[tuple[str, int]]" = []
        with self._launch_signals_lock:
            for target in targets:
                signal = self._launch_signal(target)
                if signal is None:
                    # The slot was dropped by the cap after its monitor
                    # closed it, and the debt it left behind is what
                    # carries the closed-slot handoff across the
                    # eviction. The name is read FIRST so a launch whose
                    # name can no longer be recovered does not consume
                    # the debt on its way to reporting nothing
                    # (wh-launch-signal-eviction).
                    name = self._provider_of_generation(target)
                    if name is not None and self._claim_evicted_report(
                        target
                    ):
                        orphaned.append((name, target))
                    continue
                signal.failed = True
                signal.event.set()
                if signal.closed and not signal.reported:
                    name = self._provider_of_generation(target)
                    if name is not None:
                        signal.reported = True
                        orphaned.append((name, target))
        for name, target in orphaned:
            logger.warning(
                f"Provider {name} reported a failed startup after its "
                f"startup monitor had already finished (launch "
                f"generation {target})"
            )
            if not self._report_stopped_off_loop(name, target):
                self._restore_unspent_report(target)
        with self._launch_signals_lock:
            # A failure from a launch that has already been replaced
            # must not end the starting state of the launch that
            # replaced it (wh-launch-generation). The flag is written
            # under the same lock because start_provider resets it in
            # the same stamp block it clears the event in, so leaving it
            # outside would let a replacement's reset be overwritten by
            # a failure it does not own (wh-launch-generation.2.14).
            self._provider_startup_failed = True
            self._end_starting_state(generation)
        logger.debug(
            "Provider startup-failed signal received "
            f"(launch generation {generation})"
        )

    def launch_generation(self, provider_name: str) -> Optional[int]:
        """The generation of the latest launch of `provider_name`.

        Returns None when this launcher never started that provider.
        The engine record stores this alongside the name, so a later
        report from an earlier launch of the same provider can be
        refused (wh-launch-generation).
        """
        return self._launch_generations.get(provider_name)

    def record_undeclared_startup_failure(
        self, provisional_generation: Optional[int]
    ) -> None:
        """Record a startup failure that no launch can be given.

        Called by WebSocketManager when a connection reports
        kind="startup_failed" but never declared which provider it is,
        so its launch stamp is still the connect-time guess. Attributing
        the failure to that guess is the defect wh-launch-generation.1.2
        removed, and guessing by name would put name identity back -- so
        the failure is dropped as a signal and recorded here as
        evidence instead.

        The launch that was starting when the connection arrived is the
        one that reads it. That launch's monitor treats a live
        subprocess and no signal as a slow cold start (wh-v0q), and
        without this record that assumption left a provider that
        reported a configuration failure and stayed up shown as running
        for the rest of the session (wh-launch-generation.2.3).

        Args:
            provisional_generation: The connect-time stamp of the
                connection that reported the failure. None when the
                connection was never stamped, in which case there is
                nothing to record.
        """
        if provisional_generation is None:
            return
        logger.warning(
            "Recorded an unattributable startup failure against launch "
            f"generation {provisional_generation}"
        )
        orphan: Optional[str] = None
        with self._launch_signals_lock:
            self._undeclared_startup_failures.add(provisional_generation)
            # The launch's monitor reads this record once, at its
            # deadline, and then closes its slot. A record written after
            # that read has nobody left to consult it, so this thread
            # reports the stop itself -- the same handoff the failure
            # signal makes, for the same reason
            # (wh-launch-generation.2.4).
            signal = self._launch_signal(provisional_generation)
            name = self._provider_of_generation(provisional_generation)
            if signal is None:
                # The slot went with the cap after its monitor closed
                # it. The record written just above is read at exactly
                # one place, the monitor's deadline, so leaving it here
                # would strand it for the life of the launcher AND skip
                # the orphan report. Both halves are handled together:
                # the debt is claimed, or there is no debt and the
                # record has nobody left to reach either way
                # (wh-launch-signal-eviction).
                self._undeclared_startup_failures.discard(
                    provisional_generation
                )
                if name is not None and self._claim_evicted_report(
                    provisional_generation
                ):
                    orphan = name
            elif signal.closed:
                # The record has exactly one reader -- the monitor at its
                # deadline, immediately before it closes this slot -- so
                # a closed slot means the record can never be read again,
                # and it goes whatever else happens here. Tying the
                # discard to making the report stranded it for the life
                # of the launcher in the two cases where no report is
                # made: a launch the monitor reported itself, and one no
                # longer nameable (wh-launch-signal-eviction.1.2).
                self._undeclared_startup_failures.discard(
                    provisional_generation
                )
                if not signal.reported and name is not None:
                    signal.reported = True
                    orphan = name
        if orphan is None:
            return
        logger.warning(
            f"Provider {orphan} reported a failed startup it could not "
            f"name, after its startup monitor had already finished "
            f"(launch generation {provisional_generation})"
        )
        # is_starting must end, or the startup suppression swallows
        # every later notice from this provider
        # (wh-google-creds-file-picker.1.5). Only for the CURRENT
        # launch, and comparison and write in one step
        # (wh-launch-generation.2.6, .2.14).
        self._end_starting_state(provisional_generation)
        if not self._report_stopped_off_loop(orphan, provisional_generation):
            self._restore_unspent_report(provisional_generation)

    def _provider_of_generation(self, generation: int) -> Optional[str]:
        """The provider whose latest launch is `generation`.

        Returns None once a later launch of that provider has replaced
        the generation in the map, which is the only case where the name
        cannot be recovered. A report for a superseded launch would be
        refused by the engine record anyway, so losing the name there
        costs nothing (wh-launch-generation.2.2).
        """
        for name, stamped in self._launch_generations.items():
            if stamped == generation:
                return name
        return None

    def current_launch_generation(self) -> Optional[int]:
        """The generation of the most recent launch of any provider.

        This is a PROVISIONAL stamp for a connection that has not yet
        said which provider it is. A provider connects seconds after its
        own spawn, so a slow provider from an earlier launch can connect
        while a later launch is starting and be stamped with that later
        launch. WebSocketManager corrects the stamp from the provider's
        own capabilities message (wh-launch-generation.1.2).
        """
        return self._current_launch_generation

    def _launch_signal(self, generation: Optional[int]) -> "Optional[_LaunchSignal]":
        """The signal slot for one launch, for a reader of that launch.

        Answers None in two cases, and creates a slot in neither.
        Nothing to name: `generation` is None. And a launch whose slot
        the cap has since dropped, which is every generation the counter
        has already reached that the ring no longer holds -- building a
        fresh slot for one of those would hand back an object nobody
        reads and drop a live launch's slot to make room for it
        (wh-launch-signal-eviction).

        A launch's own slot is created by `_open_launch_signal`, in the
        same locked step that issues the generation. The create branch
        left here serves a generation ABOVE the counter, which no
        launch of this launcher has issued.

        A slot is read after its monitor has finished -- that is how a
        late failure learns that nobody is left to report it
        (wh-launch-generation.1.1, .2.2, .2.4).
        """
        if generation is None:
            return None
        with self._launch_signals_lock:
            signal = self._launch_signals.get(generation)
            if signal is not None:
                return signal
            if generation <= self._launch_generation_counter:
                # Issued, and since dropped by the cap. `start_provider`
                # creates the slot in the SAME locked step that issues
                # the generation, and the eviction below is the only
                # site that removes one, so an issued generation with no
                # slot was evicted -- there is no third way to reach
                # here. Nobody is left to read a slot built now, and
                # building one would drop a live launch's slot to make
                # room for it. The launches still owed a report are
                # tracked separately (wh-launch-signal-eviction).
                return None
            signal = _LaunchSignal()
            self._launch_signals[generation] = signal
            self._evict_unowned_slots(protect=generation)
            return signal

    def _open_launch_signal(self, generation: int) -> "_LaunchSignal":
        """Create the slot for a launch that has just been stamped.

        The caller holds the signal lock.

        `_launch_signal` refuses to build a slot for a generation the
        counter has already reached, because such a generation was
        issued and then evicted, and nobody is left to read a new one.
        `start_provider` is the one place that ISSUES a generation, so
        it is the one place that may create its slot -- and it must,
        because the refusal would otherwise leave every launch with no
        slot at all (wh-launch-signal-eviction).
        """
        signal = _LaunchSignal()
        self._launch_signals[generation] = signal
        self._evict_unowned_slots(protect=generation)
        return signal

    def _evict_unowned_slots(self, protect: Optional[int] = None) -> None:
        """Drop the oldest slots nobody is holding, down to the cap.

        The caller holds the signal lock.

        `protect` names a slot that was created moments ago and whose
        owner has not taken its hold yet. Without it the new slot is the
        only unowned one in a ring of held slots, so it is the one the
        scan below finds and drops -- the launch evicts ITSELF, ends up
        with no slot at all, and its outcome is lost exactly as it was
        before this change (wh-launch-signal-eviction).

        A slot with a live owner is skipped rather than dropped: the
        owner is waiting on THAT object, so dropping it strands the
        owner for its whole timeout and sends the launch's own outcome
        to a slot nothing reads (wh-launch-signal-eviction).

        crewcut: when every slot in the ring is owned this drops
        nothing, and the ring exceeds `_MAX_TRACKED_LAUNCH_SIGNALS`. The
        growth is bounded rather than unbounded -- an owned slot means a
        live startup monitor or late-death watch, and each of those ends
        within its startup timeout plus `_LATE_DEATH_WATCH_LIMIT` -- so
        the ring holds at most the cap in abandoned slots plus one per
        launch actually in flight. Removing the excess needs a bound on
        how many launches may be in flight at once, which belongs to
        `start_provider` rather than here.
        """
        while len(self._launch_signals) > _MAX_TRACKED_LAUNCH_SIGNALS:
            oldest = None
            for generation, signal in self._launch_signals.items():
                if generation == protect:
                    continue
                if signal.owners == 0:
                    oldest = generation
                    break
            if oldest is None:
                return
            evicted = self._launch_signals.pop(oldest)
            if evicted.closed and not evicted.reported:
                self._evicted_unreported[oldest] = None
                while (
                    len(self._evicted_unreported)
                    > _MAX_TRACKED_LAUNCH_SIGNALS
                ):
                    # crewcut: the oldest debt is forgotten here, so a
                    # failure arriving after another cap's worth of
                    # launches is still lost. Removing this needs a
                    # report that does not wait for the failure to
                    # arrive, which is a decision about what to tell the
                    # user about a launch that has said nothing at all.
                    self._evicted_unreported.popitem(last=False)

    def _claim_launch_signal(self, generation: Optional[int]) -> None:
        """Take a hold on a launch's slot, so the cap cannot drop it.

        The caller holds the signal lock. Two parties claim, and each
        releases exactly once: `start_provider` claims in the same
        locked step that stamps the launch, and `_monitor_startup`
        releases that claim in its `finally`; `_monitor_startup` claims
        again for the late-death watch before starting it, and the watch
        releases that one in its own `finally`.

        The monitor's claim is made by `start_provider` rather than by
        the monitor thread because the Popen, the port-file write and
        the thread start all sit between the stamp and the monitor's
        first line -- a claim taken there would leave the slot droppable
        across that whole span (wh-launch-signal-eviction).
        """
        if generation is None:
            return
        signal = self._launch_signals.get(generation)
        if signal is not None:
            signal.owners += 1

    def _release_launch_signal(self, generation: Optional[int]) -> None:
        """Give up a hold taken by `_claim_launch_signal`.

        Safe for a generation whose slot has already gone: the count
        lives on the slot rather than on the launcher, so a slot that is
        not there took its claims with it.
        """
        if generation is None:
            return
        with self._launch_signals_lock:
            signal = self._launch_signals.get(generation)
            if signal is not None and signal.owners > 0:
                signal.owners -= 1
                if signal.owners == 0:
                    # The hold keeping this slot past the cap has gone,
                    # so make room now rather than at the next launch: a
                    # just-released slot is exactly the kind the cap
                    # exists to drop.
                    self._evict_unowned_slots()

    def _claim_evicted_report(self, generation: Optional[int]) -> bool:
        """Claim the report a launch was still owed when its slot went.

        The caller holds the signal lock. True when this caller now owns
        that report. The debt is removed as it is claimed, so a launch is
        still reported exactly once after its slot has gone -- the
        guarantee `signal.reported` gives while the slot is alive
        (wh-launch-generation.2.5, wh-launch-signal-eviction).
        """
        if generation is None or generation not in self._evicted_unreported:
            return False
        del self._evicted_unreported[generation]
        return True

    def launch_is_current(self, generation: Optional[int]) -> bool:
        """True while `generation` is still the launch that is starting.

        The working dialog and the failure notice are shared by every
        provider. A monitor for a launch the user has already replaced
        must not touch either: it would dismiss the new launch's loading
        display, or announce "Failed to start" for a provider that is
        starting normally (wh-launch-generation.2.7).

        Public because the monitor is not the only party that reaches
        that shared display: WebSocketManager acts on a provider's own
        `startup_failed` frame and asks the same question there
        (wh-launch-generation.2.8).

        crewcut: read as late as possible rather than held under a
        lock, so a restart landing between this answer and the call it
        guards is not closed here. Both halves that window used to cost
        are now answered elsewhere, and only one of them is fully shut.

        The dialog half is shut. A dismiss carries the launch it
        belongs to and the GUI drops one addressed to a launch that no
        longer owns the dialog, so the queue that has already ordered
        the replacement's show against this hide is what decides
        (wh-launch-addressed-notices).

        The notice half is narrowed, not shut. `_notify` reads this
        method again immediately before it calls the notification
        callback, so the window that remains lies between that read and
        the notification itself -- a few instructions, rather than the
        dismiss and the queue put that used to run in between. A launch
        replaced inside that window can still be told "Failed to start
        - try restarting Wheelhouse" for a provider that is starting
        normally.

        A lock is NOT the way to close what is left. _hide_working is a
        queue put_nowait and would be safe to hold, but _notify ends in
        a Win32 notification of unbounded duration; holding this
        class's lock across it would stall start_provider and the
        event-loop thread on that notification, which is a worse defect
        than the race. Closing the rest needs the notification itself
        to carry its launch, so the party that displays it can drop one
        whose launch has been replaced.
        """
        return generation is None or generation == self._current_launch_generation

    def rearm_watchdog(self, provider_name: str) -> None:
        """Watch this provider's live launch again after an unlanded stop.

        `stop_provider` disables recovery before its first await and
        nothing puts that intent back, so an engine that outlives an
        undelivered shutdown is never watched (wh-codex-merge-audit.8.1.1).
        """
        with self._launch_signals_lock:
            signal = self._launch_signal(self._launch_generations.get(provider_name))
            if signal is not None:
                signal.stop_requested = False

    def _end_starting_state(self, generation: Optional[int]) -> None:
        """End the shared starting state, but only for `generation`.

        `is_starting` is one flag shared by every provider, so a launch
        that has been replaced must not end the replacement's startup:
        the websocket manager's startup suppression would stop holding
        the replacement's notices back while it is still loading
        (wh-launch-generation.2.6).

        The comparison and the write are ONE step, under the lock
        `start_provider` also takes across its stamp. Read separately
        they were a check-then-act: a switch landing between them passed
        the comparison against the launch it replaced and then set the
        replacement's event, which is .2.6 reopened through a gap rather
        than through a missing guard (wh-launch-generation.2.14).

        A lock is the right instrument HERE and was the wrong one for
        `launch_is_current`. Everything held across is non-blocking --
        one integer comparison and a `threading.Event` write -- whereas
        that method gates `_notify`, which ends in a Win32 notification
        of unbounded duration. The lock is an RLock, and `_launch_signal`
        acquires it too, so `start_provider` re-entering it is safe.
        """
        with self._launch_signals_lock:
            if self.launch_is_current(generation):
                self._provider_ready_event.set()

    @property
    def is_starting(self) -> bool:
        """True while a provider startup is in progress (before 'ready' received)."""
        return not self._provider_ready_event.is_set()

    def _monitor_startup(
        self,
        provider_name: str,
        display_name: str,
        timeout: float = DEFAULT_STARTUP_TIMEOUT,
        generation: Optional[int] = None,
        process: "Optional[subprocess.Popen]" = None,
    ) -> None:
        """Run the startup monitor and give its slot back afterwards.

        `start_provider` takes a hold on this launch's signal slot in
        the same locked step that stamps the launch, because the Popen,
        the port-file write and this thread's own start all sit between
        the stamp and the body's first line -- a hold taken inside the
        body would leave the slot droppable across that whole span
        (wh-launch-signal-eviction).

        The release is in a `finally` so a body that raises cannot leave
        a slot pinned in the ring for the life of the launcher. It is a
        wrapper rather than a `try` inside the body because wrapping the
        body in place would re-indent every line of it, and the mutation
        gate matches exact source text inside that method.

        The generation is resolved HERE, once, so the release names the
        same launch the body reads. The body keeps its own fallback for
        the direct callers that reach it with nothing stamped.
        """
        if generation is None:
            generation = self._launch_generations.get(provider_name)
        try:
            self._monitor_startup_body(
                provider_name, display_name, timeout, generation, process
            )
        finally:
            self._release_launch_signal(generation)

    def _monitor_startup_body(
        self,
        provider_name: str,
        display_name: str,
        timeout: float = DEFAULT_STARTUP_TIMEOUT,
        generation: Optional[int] = None,
        process: "Optional[subprocess.Popen]" = None,
    ) -> None:
        """Monitor provider startup in background thread.

        Waits for either:
        1. The provider to send a 'ready' notification via WebSocket
        2. The timeout to expire

        If timeout expires without receiving ready signal, sends failure notification.

        Args:
            provider_name: Internal provider name for PID file lookup.
            display_name: User-friendly name for notifications.
            timeout: Maximum seconds to wait for startup.
            process: The child this launch spawned. `_subprocesses` is
                keyed by provider NAME and start_provider overwrites
                that key on every restart, so reading it there answered
                for whichever launch of the provider is the most recent
                one -- a monitor could give its own dead launch the
                slow-start benefit that belonged to the launch that
                replaced it (wh-launch-generation.2.7). None falls back
                to the name-keyed handle, for direct callers that have
                no child to name.
            generation: The launch this monitor supervises. Every path
                that spawns a provider passes it explicitly, so the
                default below is for direct callers only and a spawn
                path must not rely on it: the default is read after
                this thread has started, so a re-stamp of the same
                provider in between would hand the monitor a LATER
                launch than the one it supervises -- the same mistake
                the connect-time stamp made
                (wh-launch-generation.1.2).
        """
        # The starting state (event cleared, failure flag reset) was
        # already established by start_provider BEFORE the subprocess
        # was spawned. Resetting here instead would erase a ready or
        # startup-failed signal that a fast provider delivered between
        # the spawn and this thread starting
        # (wh-google-creds-file-picker.1.10).
        if generation is None:
            generation = self._launch_generations.get(provider_name)

        # Wait on THIS launch's own signal slot. A failure reported by
        # a launch that has already been replaced sets that launch's
        # slot and never this one, so no monitor can be woken, or
        # silenced, by a signal that is not its own
        # (wh-launch-generation.1.1).
        signal = self._launch_signal(generation)
        if signal is None:
            # No launch to name -- only reachable when nothing stamped
            # one. Fall back to the shared event, which is what every
            # monitor used before the generation existed.
            ready = self._provider_ready_event.wait(timeout=timeout)
            failed = self._provider_startup_failed
        else:
            signal.event.wait(timeout=timeout)

        # Before crying wolf, check whether the subprocess we spawned is
        # still alive -- GPU cold-start can easily exceed the
        # ready-signal timeout while the provider is perfectly healthy
        # and will transcribe correctly shortly after (wh-v0q).
        proc = process if process is not None else self._subprocesses.get(
            provider_name
        )
        subprocess_alive = proc is not None and proc.poll() is None

        undeclared_failure = False
        if signal is not None:
            with self._launch_signals_lock:
                # Read the SLOT, not the wait's answer. A failure that
                # sets the slot at the deadline leaves the wait
                # returning False, and gating on that answer sent the
                # monitor to the timeout branch -- which is silent while
                # the subprocess is alive, so the failure was never
                # reported (wh-launch-generation.2.2).
                ready = signal.event.is_set()
                failed = signal.failed
                # A connection reported a failed startup while this
                # launch was the one starting, but never said which
                # provider it was, so the websocket manager could not
                # attribute it and dropped it. The silence below is
                # therefore NOT a slow cold start, and treating it as
                # one left the provider shown as running for the rest of
                # the session (wh-launch-generation.2.3). The record is
                # taken whatever the outcome, so a launch that went on
                # to start cleanly does not leave it behind.
                if generation is not None:
                    undeclared_failure = (
                        generation in self._undeclared_startup_failures
                    )
                    self._undeclared_startup_failures.discard(generation)
                # The slot stays in the ring rather than being removed
                # here, so a failure arriving after this point finds it
                # and can see that its monitor has gone. The cap still
                # evicts it (wh-launch-generation.1.1, .2.2).
                signal.closed = True
                # The whole outcome is decided HERE, under the lock, and
                # the report is claimed only by the party that will make
                # it. Claiming it later -- at each report site -- left a
                # window in which a failure signal saw a closed,
                # unclaimed slot, reported the launch without waiting,
                # and the monitor then reported the same launch again;
                # the second report's survivor write was refused because
                # the first had already cleared the record, so the tray
                # stayed stopped while another engine was transcribing
                # (wh-launch-generation.2.5).
                if ready:
                    will_report = failed
                else:
                    will_report = undeclared_failure or not subprocess_alive
                if will_report:
                    signal.reported = True

        if ready:
            if failed:
                # The provider reported a FAILED startup
                # (kind="startup_failed"); it already told the user
                # exactly what is wrong, so no generic notification --
                # just close the working dialog
                # (wh-google-creds-file-picker.1.5).
                logger.warning(
                    f"Provider {provider_name} reported a failed startup"
                )
                if self.launch_is_current(generation):
                    self._hide_working(generation)
                # The failure that reaches here belongs to THIS launch:
                # the wait above is on this launch's own slot, so a
                # startup_failed from the provider being replaced can no
                # longer blank the display for the replacement
                # (wh-launch-generation, from the deferral on
                # wh-remote-stt-robustness.2.3).
                self._provider_stopped(
                    provider_name, may_wait=True, generation=generation
                )
                return
            logger.debug(f"Provider {provider_name} startup confirmed via ready notification")
            # No failure notification needed - provider already sent "ready" toast
            return

        # Timeout reached without ready signal. The two facts the
        # branches below turn on were read differently, and the
        # difference matters. Whether a dropped failure was recorded
        # against this launch was read under the lock that decided who
        # reports (wh-launch-generation.2.5). Whether the subprocess is
        # alive was NOT: it is the poll above, taken before that lock.
        #
        # The liveness answer is therefore already stale by the time it
        # is used, and moving the poll inside the lock would not change
        # that -- the child can exit one instruction after the lock as
        # easily as one before it. Narrowing that window was never the
        # answer. The gap underneath it was: the slow-start branch
        # returns without reporting whenever the child is alive at the
        # deadline (wh-v0q), which left the user with a dead engine
        # selected, no failure notice, and a dialog that had already
        # closed. The branch now hands the child to a watch that
        # outlives this monitor, so a stale answer here costs only the
        # poll interval before that watch sees the exit
        # (wh-launch-generation.2.10, wh-provider-late-death-watch).
        if subprocess_alive and not undeclared_failure:
            logger.warning(
                f"Provider {provider_name} did not send ready notification within {timeout}s, "
                f"but subprocess is still alive. Suppressing failure notification -- "
                f"provider is likely still warming up (wh-v0q)."
            )
            # Hide the working dialog so the UI doesn't hang forever. The provider
            # will still send its own ready/error notifications when it finishes
            # warming up.
            if self.launch_is_current(generation):
                self._hide_working(generation)
            # End this launch's starting state, or it never ends. The
            # dialog is already hidden, so the user believes startup is
            # over, but `is_starting` stayed true for the rest of the
            # session and the websocket manager's startup suppression
            # then swallowed every later non-exempt notice from this
            # provider. The provider's own ready is the only other path
            # that ends it, and a provider whose ready was dropped as
            # undeclared, or never sent one, never takes that path
            # (wh-ready-connection-stamp.2).
            #
            # Before the watch, not after: a refused thread start raises
            # out of this branch, and the starting state must not depend
            # on the watch being built. `_end_starting_state` compares
            # the generation under the lock, so a monitor whose launch
            # has been replaced cannot end the replacement's startup
            # (wh-launch-generation.2.6).
            self._end_starting_state(generation)
            # Nothing else is left watching this child, so hand it to a
            # watch of its own. A thread rather than a loop here, because
            # this method's contract is that it returns at its deadline
            # (wh-provider-late-death-watch).
            if signal is not None and proc is not None:
                watch = threading.Thread(
                    target=self._watch_for_late_death,
                    args=(
                        provider_name,
                        display_name,
                        signal,
                        proc,
                        generation,
                    ),
                    daemon=True,
                )
                with self._launch_signals_lock:
                    # Finished watches are dropped here rather than
                    # joined, for the reason _report_stopped_off_loop
                    # gives over the same filter: nothing waits on them
                    # outside the tests, and a launcher that ran for
                    # hours must not accumulate handles. A slow start is
                    # the cold-start case (wh-v0q), so these arrive for
                    # as long as the session lasts
                    # (wh-provider-late-death-watch.1.5).
                    self._late_death_watch_threads = [
                        existing
                        for existing in self._late_death_watch_threads
                        if existing.is_alive()
                    ]
                    self._late_death_watch_threads.append(watch)
                    # The watch holds this same slot object for
                    # `_LATE_DEATH_WATCH_LIMIT` after this method
                    # returns, so it takes a hold of its own. Under the
                    # lock the append already holds, so no launch can
                    # land between the hold and the start. Released by
                    # the watch's own `finally`
                    # (wh-launch-signal-eviction).
                    self._claim_launch_signal(generation)
                try:
                    watch.start()
                except BaseException:
                    # The hold above is released by the watch's own
                    # `finally`, which a thread that never ran never
                    # reaches. A leaked hold is permanent -- every
                    # eviction pass skips a held slot -- so one refused
                    # thread start would raise the ring's floor for the
                    # life of the launcher. Give the hold back here and
                    # let the exception carry on to the wrapper's
                    # `finally`, which releases the monitor's own hold
                    # (wh-launch-signal-eviction.1.1). The unstarted
                    # handle needs no cleanup: the `is_alive()` filter
                    # above drops it at the next append.
                    self._release_launch_signal(generation)
                    raise
        elif undeclared_failure:
            logger.warning(
                f"Provider {provider_name} did not send ready notification "
                f"within {timeout}s, and a connection reported a failed "
                f"startup against this launch without naming its provider. "
                f"Reporting it stopped rather than waiting on a provider "
                f"that already said it failed (wh-launch-generation.2.3)."
            )
            if self.launch_is_current(generation):
                self._hide_working(generation)
            # The provider's own message already told the user what is
            # wrong, so no generic notification here -- the same reason
            # the failure branch above stays quiet
            # (wh-google-creds-file-picker.1.5).
            # is_starting must end too, or the startup suppression
            # swallows every later notice from this provider
            # (wh-google-creds-file-picker.1.5). Only for the CURRENT
            # launch: consuming a record for a launch that has been
            # replaced must not end the replacement's startup, which is
            # the guard the failure signal already makes
            # (wh-launch-generation.2.6). Comparison and write are one
            # step (wh-launch-generation.2.14).
            self._end_starting_state(generation)
            self._provider_stopped(
                provider_name, may_wait=True, generation=generation
            )
        else:
            logger.warning(
                f"Provider {provider_name} did not send ready notification within {timeout}s "
                f"and subprocess is not alive."
            )
            if self.launch_is_current(generation):
                self._hide_working(generation)
                self._notify(
                    display_name,
                    "Failed to start - try restarting Wheelhouse",
                    generation,
                )
            # The starting state ends here for the same reason it ends
            # in the failure branch above: left true, it outlives this
            # launch, and the websocket manager's startup suppression
            # swallows every later non-exempt notice from whatever
            # provider is connected until the next start_provider
            # (wh-google-creds-file-picker.1.5). Only for the CURRENT
            # launch, through the same guarded call
            # (wh-ready-connection-stamp.2.1.2).
            self._end_starting_state(generation)
            self._provider_stopped(
                provider_name, may_wait=True, generation=generation
            )

    def _watch_for_late_death(
        self,
        provider_name: str,
        display_name: str,
        signal: "_LaunchSignal",
        proc: "subprocess.Popen",
        generation: Optional[int],
    ) -> None:
        """Run the late-death watch and give its slot back afterwards.

        The hold was taken by `_monitor_startup` before this thread
        started, so the slot could not be dropped in the gap between the
        two (wh-launch-signal-eviction). A wrapper for the same two
        reasons the startup monitor has one: the release belongs in a
        `finally`, and wrapping the body in place would re-indent every
        line the mutation gate matches inside it.
        """
        try:
            self._watch_for_late_death_body(
                provider_name, display_name, signal, proc, generation
            )
        finally:
            self._release_launch_signal(generation)

    def _watch_for_late_death_body(
        self,
        provider_name: str,
        display_name: str,
        signal: "_LaunchSignal",
        proc: "subprocess.Popen",
        generation: Optional[int],
    ) -> None:
        """Report a pre-ready child that exits after its monitor gave up.

        The startup monitor decides its whole outcome at its deadline,
        and the slow-start branch is the one that decides nothing: a
        child still alive there is assumed to be warming up (wh-v0q), so
        the monitor hides the working dialog and returns without
        claiming a report. Nothing then watched that child, so a
        provider dying a moment later left the user with a dead engine
        selected, no failure notice, and a dialog that had already
        closed (wh-launch-generation.2.10).

        This is that missing watcher. It ends three ways. The launch's
        own slot being set means the provider went ready or declared its
        own failure, and whichever party set it owns the outcome, so the
        watch returns silently -- that is what keeps the slow-start
        benefit intact. A non-None exit code before that is the late
        death, reported below. The limit ends a watch over a child that
        neither speaks nor exits.

        The report is CLAIMED under the lock before it is made. The
        monitor left this slot closed and unclaimed, which is how a late
        `startup_failed` learns that nobody is left to report the launch
        and reports it itself (wh-launch-generation.2.2). This watch
        reaches the same slot, so without the claim the two could both
        report one launch: the second report's survivor write is refused
        because the first already cleared the record, and the tray then
        shows stopped while another engine is transcribing
        (wh-launch-generation.2.5).

        Nothing blocking is held under that lock -- two boolean reads,
        the slot's own event and `signal.reported`, and one boolean
        write. The poll, the wait, and the report itself all happen
        outside it, for the reason `launch_is_current` records:
        `_notify` ends in a Win32 notification of unbounded duration,
        and holding this class's lock across it would stall
        `start_provider` and the event loop thread
        (wh-launch-generation.2.9).
        """
        poll_interval = self._late_death_poll_interval
        deadline = time.monotonic() + self._late_death_watch_limit
        while True:
            if signal.event.wait(timeout=poll_interval):
                # Ready, or a failure the provider declared itself.
                # Either way that path owns the outcome, not this one.
                return
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                # crewcut: a child that has neither become ready nor
                # exited this long after its startup timeout is the
                # wh-v0q slow start taken to its limit, not a late
                # death, and this watch has nothing true to say about
                # it. Removing the limit needs a liveness report from
                # the provider itself, so that silence and progress can
                # be told apart rather than waited out.
                logger.warning(
                    f"Stopped watching {provider_name} (launch generation "
                    f"{generation}): it has neither sent ready nor exited "
                    f"within {self._late_death_watch_limit}s of its startup "
                    f"timeout"
                )
                return

        with self._launch_signals_lock:
            if signal.event.is_set():
                # The slot was read at the top of the loop and the poll
                # came after it, so a ready that landed in between is
                # invisible to that read. `signal_provider_ready` sets
                # the slot without claiming the report, so the check
                # below cannot see it either, and the launch that just
                # announced itself would be called a startup failure.
                # The gap is a thread-scheduling gap, not a few
                # instructions (wh-provider-late-death-watch.2.3).
                return
            if signal.reported:
                # A late startup_failed frame reached this launch first
                # and has already reported it.
                return
            signal.reported = True

        logger.warning(
            f"Provider {provider_name} exited before sending ready, after its "
            f"startup monitor had already given up (launch generation "
            f"{generation})"
        )
        # The display and the report an in-window death gets. The
        # starting state is not touched here: this launch's monitor has
        # already returned through the slow-start branch, which ended it
        # (wh-ready-connection-stamp.2), and the dead-child branch above
        # now ends it for an in-window death (wh-ready-connection-
        # stamp.2.1.2), so by the time this watch fires there is nothing
        # left of this launch's starting state to end
        # (wh-provider-late-death-watch).
        if self.launch_is_current(generation):
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
        self._provider_stopped(
            provider_name, may_wait=True, generation=generation
        )

    def discover_providers(self) -> list[dict]:
        """Scan services directory for STT providers.

        Providers are identified by having a config.toml file with a
        [provider] section containing name, display_name, and launcher fields.

        Returns:
            List of provider info dicts with keys: name, display_name, launcher, service_dir
        """
        providers = []

        if not self.services_dir.exists():
            logger.warning(f"Services directory not found: {self.services_dir}")
            return providers

        for service_dir in self.services_dir.iterdir():
            if not service_dir.is_dir():
                continue

            config_path = service_dir / "config.toml"
            if not config_path.exists():
                continue

            try:
                with open(config_path, "rb") as f:
                    config = tomllib.load(f)

                # Check for [provider] section
                provider_section = config.get("provider")
                if not provider_section:
                    continue

                # Extract required fields
                name = provider_section.get("name")
                display_name = provider_section.get("display_name")
                launcher = provider_section.get("launcher")

                if not all([name, display_name, launcher]):
                    logger.warning(
                        f"Incomplete [provider] section in {config_path}: "
                        f"name={name}, display_name={display_name}, launcher={launcher}"
                    )
                    continue

                # Skip disabled providers
                if not provider_section.get("enabled", True):
                    logger.debug(f"Skipping disabled provider: {name}")
                    continue

                # Skip templates (not actual providers)
                if provider_section.get("template", False):
                    logger.debug(f"Skipping template provider: {name}")
                    continue

                # Verify launcher exists
                launcher_path = service_dir / launcher
                if not launcher_path.exists():
                    logger.warning(
                        f"Launcher not found for provider {name}: {launcher_path}"
                    )
                    continue

                # Optional per-provider startup timeout override (wh-v0q).
                # Falls back to DEFAULT_STARTUP_TIMEOUT if unset or invalid.
                startup_timeout = provider_section.get("startup_timeout_seconds")
                if not isinstance(startup_timeout, (int, float)) or startup_timeout <= 0:
                    startup_timeout = DEFAULT_STARTUP_TIMEOUT

                # Resolve {mode} placeholder up-front so the tray menu and
                # every downstream consumer see the same CPU/GPU-resolved
                # string (the menu populates from discovery, not from
                # start_provider() which was previously the only call site).
                if "{mode}" in display_name:
                    display_name = self._resolve_display_name(display_name, service_dir)

                providers.append({
                    "name": name,
                    "display_name": display_name,
                    "launcher": launcher,
                    "service_dir": service_dir,
                    "startup_timeout_seconds": float(startup_timeout),
                })
                logger.debug(f"Discovered provider: {name} at {service_dir}")

            except Exception as e:
                logger.error(f"Error reading config from {config_path}: {e}")
                continue

        self._providers = providers
        logger.info(f"Discovered {len(providers)} STT providers")
        return providers

    def get_provider_by_name(self, name: str) -> Optional[dict]:
        """Get provider info by name.

        Args:
            name: Provider name (e.g., "google_stt", "parakeet_tdt").

        Returns:
            Provider info dict, or None if not found.
        """
        if self._providers is None:
            self.discover_providers()

        for provider in self._providers:
            if provider["name"] == name:
                return provider
        return None

    def _get_pid_file_path(self, provider_name: str) -> Path:
        """Get the PID file path for a provider.

        Args:
            provider_name: Provider name.

        Returns:
            Path to the PID file.
        """
        return self.app_data_dir / f"{provider_name}.pid"

    def is_running(self, provider_name: str) -> bool:
        """Check if a provider is currently running.

        A provider counts as running when the supervisor subprocess this
        launcher spawned is still alive, OR when the provider's PID file
        names a live process. The first check matters because the PID
        file is written by the child launcher only after its own uv
        bootstrap: for that whole window the PID file does not exist,
        and a PID-file-only answer would let a second serialized
        lifecycle command launch a duplicate supervisor for the same
        provider (wh-google-creds-file-picker.1.22). Cleans up stale
        PID files if the process they name is dead.

        Args:
            provider_name: Provider name.

        Returns:
            True if provider is running (or its launch is in flight),
            False otherwise.
        """
        proc = self._subprocesses.get(provider_name)
        if proc is not None and proc.poll() is None:
            return True

        pid_file = self._get_pid_file_path(provider_name)

        if not pid_file.exists():
            return False

        try:
            pid = int(pid_file.read_text().strip())
            if psutil.pid_exists(pid):
                return True
            else:
                # Stale PID file - process is dead
                logger.debug(f"Removing stale PID file for {provider_name}")
                pid_file.unlink()
                # Also clean up stale port file
                port_file = self.app_data_dir / f"{provider_name}.port"
                try:
                    port_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return False
        except (ValueError, OSError) as e:
            logger.warning(f"Error reading PID file for {provider_name}: {e}")
            try:
                pid_file.unlink()
            except OSError:
                pass
            return False

    def _terminate_stale_provider(self, provider_name: str) -> None:
        """Terminate a stale provider process from a previous WheelHouse session.

        Args:
            provider_name: Provider name (for PID/port file lookup).
        """
        pid_file = self._get_pid_file_path(provider_name)
        port_file = self.app_data_dir / f"{provider_name}.port"

        try:
            pid = int(pid_file.read_text().strip())
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            logger.info(f"Terminated stale provider {provider_name} (PID {pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, OSError) as e:
            logger.debug(f"Could not terminate stale provider {provider_name}: {e}")

        # Clean up files
        for f in (pid_file, port_file):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def _report_watchdog_stopped(
        self, provider_name: str, generation: Optional[int]
    ) -> None:
        """Reconcile survivors off-loop, retaining a refused reporting handoff.

        A failed switch may have left the previous engine alive. The ordinary
        switch failure branch performs its own reconciliation; the watchdog
        has no such caller, including when its restart fails synchronously.
        Retry only the report if its thread cannot start, never the provider.
        """
        with self._launch_signals_lock:
            signal = self._launch_signal(generation)
            if (
                signal is None or self._watchdog_shutdown or signal.stop_requested
                or self._launch_generations.get(provider_name) != generation
            ):
                return
            signal.reported = True
            signal.watchdog_report_pending = True
        if self._report_stopped_off_loop(provider_name, generation):
            with self._launch_signals_lock:
                signal.watchdog_report_pending = False
        else:
            self._restore_unspent_report(generation)

    def check_provider_health(
        self, provider_name: str, generation: Optional[int],
        on_restarting: "Callable[[int], bool]",
    ) -> None:
        """Check the selected, owned launch from Logic's state-update loop.

        The existing three-second state updater supplies the cadence. A
        websocket disconnect says nothing about process death. Require a
        successful ready, the captured supervisor's exit, and no surviving
        PID-file process before attempting David's one automatic restart.
        Explicit stops disable recovery before their first await, including
        the stop/start gap in a provider switch. No wait or polling thread is
        added here, and callbacks never build a state dictionary off-loop.

        The launch asked about is the one the record names, which can be
        older than the newest launch of any provider, so the gates below
        compare this provider's latest launch (wh-codex-merge-audit.8.1.1).
        """
        with self._launch_signals_lock:
            signal = self._launch_signal(generation)
            if (
                generation is None
                or self._watchdog_shutdown or signal is None
                or signal.stop_requested
                or self._launch_generations.get(provider_name) != generation
            ):
                return
            report_pending = signal.watchdog_report_pending
            if not report_pending and (
                not signal.ready or signal.failed or signal.reported
            ):
                return
            proc = self._subprocesses.get(provider_name)
        if report_pending:
            self._report_watchdog_stopped(provider_name, generation)
            return
        if proc is None or proc.poll() is None or self.is_running(provider_name):
            return
        with self._launch_signals_lock:
            if (
                self._launch_generations.get(provider_name) != generation
                or self._watchdog_shutdown or signal.stop_requested
                or signal.reported or signal.failed
            ):
                return
            signal.reported = True
            retry_used = signal.watchdog_retry_used
        if retry_used:
            self._report_watchdog_stopped(provider_name, generation)
            self._notify(
                provider_name,
                "Speech provider stopped after its automatic restart - try restarting Wheelhouse",
                generation, owner=provider_name,
            )
            return
        record_refused = False

        def record_restart(new_generation):
            nonlocal record_refused
            accepted = on_restarting(new_generation)
            record_refused = not accepted
            return accepted

        started = self.start_provider(
            provider_name, _watchdog_generation=generation,
            _on_restarting=record_restart,
        )
        if (
            not started and not record_refused
            and self._launch_generations.get(provider_name) == generation
            and not self._watchdog_shutdown and not signal.stop_requested
        ):
            # Refusals before a new generation exists (unknown provider/port)
            # have no startup monitor or exception callback to report them.
            # A stamped spawn failure already reported its newer generation.
            self._report_watchdog_stopped(provider_name, generation)
            self._notify(
                provider_name,
                "Speech provider could not restart - try restarting Wheelhouse",
                generation, owner=provider_name,
            )

    def start_provider(
        self, provider_name: str, *, _watchdog_generation: Optional[int] = None,
        _on_restarting: "Optional[Callable[[int], bool]]" = None,
    ) -> bool:
        """Start a provider by launching its launcher.py subprocess.

        If the provider is already running, returns True without starting.

        Args:
            provider_name: Provider name.

        Returns:
            True if provider started or already running, False on error.
        """
        if _watchdog_generation is not None:
            with self._launch_signals_lock:
                previous = self._launch_signal(_watchdog_generation)
                if (
                    self._launch_generations.get(provider_name) != _watchdog_generation
                    or self._watchdog_shutdown or previous is None
                    or previous.stop_requested
                ):
                    return False
        provider = self.get_provider_by_name(provider_name)
        if provider is None:
            logger.error(f"Unknown provider: {provider_name}")
            return False

        # Guard: port must be assigned before starting any provider
        if self.ws_port == 0:
            logger.error("Cannot start provider: WebSocket port not assigned yet")
            return False

        # Check if already running with correct port
        if self.is_running(provider_name):
            tracked = self._subprocesses.get(provider_name)
            if tracked is not None and tracked.poll() is None:
                # A supervisor this launcher spawned is still alive; its
                # launch may not have written the PID or port file yet.
                # The stale-port handling below is for processes left
                # over from a PREVIOUS session -- terminating by PID
                # file would miss this one and a duplicate would be
                # spawned (wh-google-creds-file-picker.1.22). Within one
                # session the port never changes, so this launch is
                # current by construction.
                logger.info(
                    f"Provider {provider_name} launch from this session "
                    "is still alive - not starting a duplicate"
                )
                # Adopting this live launch re-arms its watchdog.
                self.rearm_watchdog(provider_name)
                return True
            port_file = self.app_data_dir / f"{provider_name}.port"
            try:
                stored_port = int(port_file.read_text().strip()) if port_file.exists() else None
            except (ValueError, OSError):
                stored_port = None

            if stored_port == self.ws_port:
                logger.info(f"Provider {provider_name} is already running on port {self.ws_port}")
                # Adopting this live launch re-arms its watchdog.
                self.rearm_watchdog(provider_name)
                return True
            else:
                # Port mismatch or missing - old provider from previous session
                logger.warning(
                    f"Provider {provider_name} running on port {stored_port}, "
                    f"but current port is {self.ws_port} - terminating stale process"
                )
                self._terminate_stale_provider(provider_name)

        service_dir = provider["service_dir"]
        launcher_script = service_dir / provider["launcher"]
        display_name = provider.get("display_name", provider_name)

        # Resolve {mode} placeholder for providers with CPU/GPU variants
        if "{mode}" in display_name:
            display_name = self._resolve_display_name(display_name, service_dir)

        # Clean up any stale PID file before starting
        # This ensures the monitor thread only sees a fresh PID file from this launch
        pid_file = self._get_pid_file_path(provider_name)
        if pid_file.exists():
            try:
                pid_file.unlink()
                logger.debug(f"Removed old PID file before starting {provider_name}")
            except OSError as e:
                logger.warning(f"Failed to remove old PID file: {e}")

        proc = None
        # Stamped below, just before the spawn. It stays None when the
        # failure lands before that point, and an unstamped report is
        # compared by name alone, exactly as before (wh-launch-generation).
        generation = None
        try:
            logger.info(f"Starting provider {provider_name} from {launcher_script}")
            # Start the launcher subprocess via uv to ensure correct virtualenv.
            # Each provider has its own uv project with dependencies; --directory
            # tells uv which project to resolve. Clear VIRTUAL_ENV so uv does not
            # inherit the parent process's virtualenv.
            #
            # --locked --no-sync: bootstrap has already synced each service venv,
            # so runtime launch must not sync or relock. Otherwise every provider
            # switch could hit the network, mutate venv state mid-run, or block
            # past the startup-monitor deadline. --locked also fails loudly if
            # the lockfile is out of date with pyproject.toml.
            env = os.environ.copy()
            env.pop("VIRTUAL_ENV", None)
            cmd = [
                "uv", "run", "--directory", str(service_dir),
                "--locked", "--no-sync",
                "python", str(launcher_script),
                "--ws-host", self.ws_host,
                "--ws-port", str(self.ws_port),
            ]

            # Only the google_stt launcher understands --credentials-file;
            # other providers' argument parsers would reject it.
            if provider_name == "google_stt" and self.google_credentials_file:
                cmd.extend(["--credentials-file", str(self.google_credentials_file)])

            # Append wake word config as CLI args if enabled
            if self.wake_word_config.get("enabled", False):
                ww = self.wake_word_config
                cmd.extend(["--wake-word-enabled"])
                if "keyword" in ww:
                    cmd.extend(["--wake-word-keyword", str(ww["keyword"])])
                if "sensitivity" in ww:
                    cmd.extend(["--wake-word-sensitivity", str(ww["sensitivity"])])
                if "mode" in ww:
                    cmd.extend(["--wake-word-mode", str(ww["mode"])])
                if "model_dir" in ww:
                    model_dir = self._resolve_wake_word_model_dir(
                        str(ww["model_dir"]),
                        service_dir,
                    )
                    cmd.extend(["--wake-word-model-dir", str(model_dir)])

            # Enter the starting state BEFORE spawning: a fast provider
            # can deliver its ready or startup-failed signal in the gap
            # between Popen and the monitor thread's first instruction,
            # and a reset inside the monitor would erase that signal and
            # turn a reported failure into a silent timeout
            # (wh-google-creds-file-picker.1.10).
            #
            # The launch generation is stamped here, in the same place
            # and for the same reason: it identifies the launch the
            # signals below belong to, and a signal can arrive before
            # the monitor thread runs (wh-launch-generation).
            #
            # Held under the signal lock so the stamp and the event
            # clear are one step against the three sites that compare a
            # generation and then end the starting state. Every write
            # here is non-blocking, and _launch_signal takes the same
            # RLock, so this cannot deadlock. Popen stays OUTSIDE
            # (wh-launch-generation.2.14).
            with self._launch_signals_lock:
                if _watchdog_generation is not None:
                    previous = self._launch_signal(_watchdog_generation)
                    if (
                        self._launch_generations.get(provider_name) != _watchdog_generation
                        or self._watchdog_shutdown or previous is None
                        or previous.stop_requested
                    ):
                        return False
                    # This callback only compares/writes StateManager's record
                    # under its record lock. Publish BEFORE a fast failed spawn
                    # can report the new generation. No notification, queue put,
                    # process operation or await is allowed in this callback.
                    if _on_restarting is None or not _on_restarting(self._launch_generation_counter + 1):
                        return False
                self._launch_generation_counter += 1
                generation = self._launch_generation_counter
                self._launch_generations[provider_name] = generation
                self._current_launch_generation = generation
                self._provider_ready_event.clear()
                self._provider_startup_failed = False
                new_signal = self._open_launch_signal(generation)
                new_signal.watchdog_retry_used = _watchdog_generation is not None
                # The startup monitor holds this slot for its whole
                # bounded wait, but it does not begin until after the
                # Popen and the port-file write below. Taking the hold
                # here, under the lock that stamps the launch, is what
                # stops the cap dropping the slot across that span
                # (wh-launch-signal-eviction). Released by
                # `_monitor_startup`'s `finally`, or by the handler
                # below on the one path that returns without a monitor.
                self._claim_launch_signal(generation)

            # The dialog is raised here rather than before the stamp,
            # because it now names the launch it belongs to and that
            # launch does not exist until the block above. Nothing
            # between the old position and this one blocks -- the
            # command list and, when wake words are on, one path
            # resolution -- so the dialog still appears before the
            # spawn. A failure earlier than this leaves the dialog
            # unraised and the dismiss below unstamped, which is what
            # the same failure did before: the dialog it had just
            # raised was the replacement's, and hiding it left the same
            # empty screen (wh-launch-addressed-notices).
            self._show_working(f"Loading {display_name}", generation)

            proc = subprocess.Popen(
                cmd,
                cwd=str(service_dir),
                env=env,
                # Detach from parent process
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32" else 0,
                start_new_session=True if sys.platform != "win32" else False,
            )
            # Track subprocess for liveness checks in _monitor_startup (wh-v0q)
            self._subprocesses[provider_name] = proc
            logger.info(f"Provider {provider_name} launcher started with --ws-host={self.ws_host} --ws-port={self.ws_port}")

            # Write port file so we can detect stale providers after restart
            port_file = self.app_data_dir / f"{provider_name}.port"
            try:
                port_file.write_text(str(self.ws_port))
            except OSError as e:
                logger.warning(f"Failed to write port file: {e}")

            # Start background thread to monitor startup and notify on timeout.
            # Use the per-provider startup_timeout_seconds from discovery (wh-v0q).
            provider_timeout = float(provider.get("startup_timeout_seconds", DEFAULT_STARTUP_TIMEOUT))
            monitor_thread = threading.Thread(
                target=self._monitor_startup,
                args=(
                    provider_name,
                    display_name,
                    provider_timeout,
                    generation,
                    proc,
                ),
                daemon=True,
            )
            monitor_thread.start()

            return True

        except Exception as e:
            logger.error(f"Failed to start provider {provider_name}: {e}")
            # No monitor thread runs on this path, so nothing else would
            # ever give back the hold taken at the stamp, and the slot
            # would sit in the ring past the cap for the life of the
            # launcher. A no-op when the failure landed before the stamp
            # (wh-launch-signal-eviction).
            self._release_launch_signal(generation)
            # The starting state was entered before Popen. On this path no
            # monitor thread is running, so nothing else would ever end
            # it: is_starting would read true forever and the startup
            # suppression would swallow every later provider notice
            # (wh-google-creds-file-picker.1.14).
            self._provider_ready_event.set()
            if proc is not None and proc.poll() is None:
                # A process was spawned but its startup monitor never
                # started; nothing would supervise it. Stop it rather
                # than leave it running unmanaged.
                try:
                    proc.terminate()
                except Exception as terminate_error:
                    logger.warning(
                        f"Failed to stop unmonitored provider process: "
                        f"{terminate_error}"
                    )
            self._hide_working(generation)
            self._notify(
                display_name,
                "Failed to start - try restarting Wheelhouse",
                generation,
            )
            if _watchdog_generation is not None:
                # Before a new stamp, check_provider_health still owns the
                # old generation's refusal report. Do not queue it twice.
                if generation is not None:
                    self._report_watchdog_stopped(provider_name, generation)
            else:
                self._provider_stopped(provider_name, generation=generation)
            return False

    def _resolve_wake_word_model_dir(self, model_dir_value: str, service_dir: Path) -> Path:
        """Resolve wake-word model directory for provider startup.

        Resolution order:
        1. Absolute path as-is
        2. Relative to provider service dir
        3. Relative to stt_providers root
        4. Legacy fallback: shared/<model_dir> (for default data/wake_words)
        5. Provider-relative path (even if missing, so provider can create/download)
        """
        model_dir = Path(model_dir_value)
        if model_dir.is_absolute():
            return model_dir

        provider_relative = (service_dir / model_dir).resolve()
        if provider_relative.exists():
            return provider_relative

        providers_relative = (self.services_dir / model_dir).resolve()
        if providers_relative.exists():
            return providers_relative

        shared_fallback = (self.services_dir / "shared" / model_dir).resolve()
        if shared_fallback.exists():
            logger.info(
                "Wake-word model_dir '%s' not found under %s, using shared fallback %s",
                model_dir_value,
                service_dir,
                shared_fallback,
            )
            return shared_fallback

        return provider_relative

    async def stop_provider(self, provider_name: str) -> bool:
        """Stop a provider by sending shutdown command.

        Sends shutdown command via WebSocket. The provider will exit cleanly
        and the launcher will not restart it (exit code 0).

        Args:
            provider_name: Provider name.

        Returns:
            True if shutdown sent or provider not running, False on error.
        """
        with self._launch_signals_lock:
            signal = self._launch_signal(self._launch_generations.get(provider_name))
            if signal is not None:
                signal.stop_requested = True
        provider = self.get_provider_by_name(provider_name)
        if provider is None:
            logger.error(f"Unknown provider: {provider_name}")
            return False

        # Check if running
        if not self.is_running(provider_name):
            logger.info(f"Provider {provider_name} is not running")
            return True

        if self._ws_manager is None:
            # No WebSocket connection means provider isn't connected - treat as stopped
            logger.debug("WebSocket manager not set - provider not connected, skipping shutdown")
            return True

        try:
            logger.info(f"Sending shutdown command to {provider_name}")
            await self._ws_manager.send_command_to_stt("shutdown")
            return True

        except Exception as e:
            logger.error(f"Failed to send shutdown to {provider_name}: {e}")
            return False

    async def shutdown_all_providers(self) -> dict[str, bool]:
        """Shutdown all discovered providers.

        Sends shutdown command to each running provider. Non-running providers
        are skipped (returns True for them). Continues even if individual
        providers fail to shutdown.

        Returns:
            Dict mapping provider name to shutdown success (True/False).
        """
        with self._launch_signals_lock:
            self._watchdog_shutdown = True
        if self._providers is None:
            self.discover_providers()

        if not self._providers:
            logger.info("No providers to shutdown")
            return {}

        results = {}
        for provider in self._providers:
            name = provider["name"]
            try:
                result = await self.stop_provider(name)
                results[name] = result
            except Exception as e:
                logger.error(f"Error shutting down {name}: {e}")
                results[name] = False

        return results
