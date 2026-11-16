"""Boundary harness for the numbered-overlay requirement tests.

Bead: wh-overlay-refactor-pilot.1. The requirements live in
docs/design/overlay-requirements.md; each test file named
``test_overlay_requirements_*.py`` pins one table of that file.

WHAT THIS HARNESS IS FOR. A requirement test drives the Logic process only
through the inputs the shipped Logic process receives, and it asserts only on
the outputs that leave it. Phase B of the epic moves code out of
``LogicController``; the tests must survive that move unchanged, so they never
name a private member of ``LogicController``, never patch one of its methods,
and never read its state. This file is the ONE place that knows how the
production pieces are started. When phase B moves a piece, this file changes
and the tests do not.

THE INPUTS a test can give, and nothing else:

  * spoken words, through a real ``SpeechHandler`` / ``SpeechProcessor``
    (``OverlayBoundary.say``);
  * replies to Logic's requests to the Input process, put on the real
    ``app.response_queue`` (``OverlayBoundary.reply``); pin, unpin and
    keepalive refresh requests, and the two click requests, are answered
    automatically with an ok reply built from the production schema, because
    every test would otherwise repeat them;
  * messages from the GUI process, put on a real ``multiprocessing.Queue`` that
    the production GUI command listener reads (``OverlayBoundary.send_gui``,
    or ``OverlayBoundary.send_gui_as_time_passes`` when time must pass at the
    moment Logic reads the message);
  * Win32 hook callbacks, from a fake ``OverlayFocusHookManager``
    (``OverlayBoundary.hooks``);
  * mouse clicks and wheel steps, through the pynput callbacks of a real
    ``MouseHandler`` called on a thread of their own, as the low-level mouse
    hook thread calls them (``OverlayBoundary.mouse``);
  * the desktop's answers about windows (``OverlayBoundary.desktop``);
  * the passage of time, from an offset clock (``OverlayBoundary.advance``);
  * config values, written to a tmp config file (``write_config``).

THE OUTPUTS a test can read:

  * frames Logic wrote for the Input process (``OverlayBoundary.frames``),
    de-framed the production way (``input_proc._read_envelope``);
  * dicts Logic put on the state-to-GUI queue (``OverlayBoundary.gui``);
  * Windows notifications that the production ``ErrorNotificationHandler``
    raises for an ERROR log record (``OverlayBoundary.notifications``);
  * the absence of any of these over a window (``OverlayBoundary.quiet``).

STARTUP STEPS THIS FILE RUNS IN PLACE OF main(). ``LogicController.main()`` (main.py LogicController.main) cannot
run in a test: it also starts the STT provider, the WebSocket server, the
speech provider launcher and the update check. So the harness starts, one by
one, the overlay pieces that ``main()`` starts. Each is a public member,
listed here with the main.py method that starts it in production:

  * ``NumberedOverlayController.start_overlay_focus_hooks`` -- main.py LogicController.main
    calls it through ``LogicController.overlay``. Starts the Win32 focus hooks.
    The harness replaces the class ``OverlayFocusHookManager`` in
    ``numbered_overlay.controller`` (a Win32 seam class, not ``LogicController``)
    with ``FakeFocusHooks`` first, so the callbacks the real method registers
    are the ones a test calls.
  * ``NumberedOverlayController.send_overlay_startup_reset`` -- main.py LogicController.main.
    Sends the reset_overlay message a fresh Logic owes the GUI process.
  * ``LogicController.listen_for_gui_commands`` -- main.py LogicController.main. The
    GUI command listener task, started on the harness's real GUI-to-Logic
    queue through the public ``create_task_with_error_handling``.

The overlay keepalive timer (the lease renew and the snapshot refresh) needs
no call from the harness: the controller arms it itself on entry to a painted state
(``_reconcile_overlay_keepalive_timer``, numbered_overlay/controller.py NumberedOverlayController._reconcile_overlay_keepalive_timer).
``_startup_task_coroutines`` (main.py LogicController._startup_task_coroutines) starts only the periodic state update, the update check and the
first-run notice, none of which is overlay behaviour, so the harness does not
start them.

The mouse source (wh-overlay-refresh-after-mouse) uses one public method of
``ServiceManager`` (not ``LogicController``): ``overlay_pointer_input_listener``,
which ``ServiceManager.initialize_services`` hands to the ``MouseHandler`` it
builds. ``initialize_services`` starts every service, so the harness builds the
``MouseHandler`` itself and connects it the same way. It never calls
``MouseHandler.start``: no pynput hook and no HID device is opened.

Two public methods of ``WheelHouseApp`` (not ``LogicController``) are used, as
tests/test_overlay_journeys.py uses them: ``app.start_demuxer`` and
``app.start_sender``. ``app.start()`` starts them in production (main.py
LogicController.main calls it) together with a WebSocket server no overlay test uses.

The controller is built exactly as ``_real_logic_controller`` in
tests/test_overlay_journeys.py builds it: the real constructor, real
``multiprocessing.Queue`` objects, a real ``SharedMemory`` command buffer, and
``utils.system.get_user_data_dir`` pointed at a tmp directory. That file is not
imported (importing a test module from another test module can load it twice
under two names); the pieces are copied here.
"""

from __future__ import annotations

import asyncio
import logging
import pickle
import queue as queue_mod
import re
import struct
import threading
import time as _real_time
import uuid
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from multiprocessing import Event as MpEvent
from multiprocessing import Queue as MpQueue
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from unittest.mock import patch

import input_proc
import utils.system as utils_system

import services.wheelhouse.main as main_module
import services.wheelhouse.numbered_overlay.controller as overlay_controller_module
from services.wheelhouse.app import WheelHouseApp
from services.wheelhouse.config_service import ConfigService
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.main import LogicController
from services.wheelhouse.service_manager import ServiceManager
from services.wheelhouse.state_manager import StateManager
from services.wheelhouse.ui.element_types import (
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)

_SERVICE_DIR = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG_PATH = _SERVICE_DIR / "config.toml.example"

# Each boundary test builds a real SpeechHandler, and its PatternCatalog
# compiles about 1,900 regular expressions. The re module keeps only 512
# compiled patterns, so without this every test compiles them all again
# (about 1 s of each test's call time). With room for all of them, the second
# and later tests in one process take the compiled patterns from the cache.
# crewcut: this raises a private limit of the re module for the whole test
# process; a PatternCatalog that one process builds once and every test reuses
# would remove it.
if getattr(re, "_MAXCACHE", 0) < 8192:
    setattr(re, "_MAXCACHE", 8192)


def _warm_the_pattern_cache() -> None:
    """Compile the shipped patterns once, when the harness is imported.

    Without this the first test of each run compiles them all inside its own
    call time. The catalog is built without migrate_legacy_ids, so it reads the
    shipped patterns file and writes nothing.
    """

    from speech.pattern_catalog import PatternCatalog

    PatternCatalog(str(_SERVICE_DIR / "speech" / "config" / "patterns.toml"))


_warm_the_pattern_cache()

# The Logic-to-Input commands that belong to the numbered overlay and to
# clicking. A "Logic sends nothing to Input" assertion is about these; the
# speech path's own bookkeeping frames (end_utterance and the like) are not
# overlay output.
OVERLAY_INPUT_ACTIONS = frozenset({
    "start_overlay_walk",
    "show_numbered_overlay",
    "pin_snapshot",
    "unpin_snapshot",
    "click_snapshot_item",
    "click_element",
})

# The Logic-to-GUI messages that belong to the numbered overlay. The walking
# cue (overlay_walk_cue) is left out of this set on purpose, so the tests that
# compare the whole overlay message sequence do not see it. The harness still
# records it, and the tests for LC-40 to LC-42 and AO-33 read it with
# gui_since(mark, actions={"overlay_walk_cue"}).
OVERLAY_GUI_ACTIONS = frozenset({
    "paint_overlay",
    "clear_overlay",
    "show_click_notice",
    "overlay_lease_renew",
    "reset_overlay",
})

# Requests the harness answers by itself with an ok reply built from the
# production schema. They are best-effort bookkeeping (main.py
# ``_overlay_send_pin_request``), but a paint waits behind the pin reply, so an
# unanswered pin would hold every test up for response_timeout_ms.
_AUTO_ANSWERED = frozenset({
    "pin_snapshot", "unpin_snapshot", "refresh_overlay_snapshot",
})

# Click requests the harness answers by itself with a successful click, built
# through ``ClickElementResponse``. The speech action awaits the click
# (patterns.toml ``awaits_done = true``), so an unanswered click would hold
# the utterance until response_timeout_ms.
_AUTO_CLICKED = frozenset({"click_snapshot_item", "click_element"})

_INPUT_BARRIER = "_overlay_harness_input_barrier"


# ---------------------------------------------------------------------------
# Windows on the fake desktop
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """One top-level window the fake desktop knows about."""

    hwnd: int
    pid: int
    process_name: str
    class_name: str
    create_time_s: float

    @property
    def creation_time_ms(self) -> int:
        """The creation time the way main.py stores it (milliseconds)."""

        return int(self.create_time_s * 1000)


NUMBERED_WINDOW = Window(
    hwnd=0x4A01, pid=4101, process_name="journeyapp.exe",
    class_name="JourneyMainWindow", create_time_s=1_700_000_000.0,
)
OTHER_WINDOW = Window(
    hwnd=0x5B01, pid=5201, process_name="otherapp.exe",
    class_name="OtherMainWindow", create_time_s=1_700_000_100.0,
)


class _FakeProcess:
    """What ``psutil.Process(pid)`` answers for a pid on the fake desktop."""

    def __init__(self, window: Window) -> None:
        self._window = window

    def name(self) -> str:
        return self._window.process_name

    def create_time(self) -> float:
        return self._window.create_time_s


class FakeDesktop:
    """The answers Win32 and psutil give about windows, under test control.

    Patches the Win32 calls themselves (``win32gui.GetForegroundWindow``,
    ``IsWindow``, ``GetClassName``, ``GetAncestor``,
    ``win32process.GetWindowThreadProcessId``) and ``psutil.Process``, never a
    method of ``LogicController``. A pid the desktop does not know falls
    through to the real ``psutil.Process``, so code that asks about its own
    process still works.

    ``psutil.Process(pid)`` answers for the FIRST window with that pid, so two
    windows of one process report one creation time. A test that needs the
    two windows of one pid to report different creation times passes
    ``foreground_answers_for_its_pid=True``: then the foreground window answers
    for its own pid. The default keeps the first-window answer, so no other
    test sees a change.
    """

    def __init__(
        self, windows: Iterable[Window], *,
        foreground_answers_for_its_pid: bool = False,
    ) -> None:
        self.windows = {w.hwnd: w for w in windows}
        self.foreground: int = next(iter(self.windows))
        self.foreground_answers_for_its_pid = foreground_answers_for_its_pid

    def bring_to_front(self, window: Window) -> None:
        self.windows.setdefault(window.hwnd, window)
        self.foreground = window.hwnd

    def install(self, stack: ExitStack) -> None:
        import psutil
        import win32gui
        import win32process

        real_process = psutil.Process

        def by_pid(pid):
            front = self.windows.get(self.foreground)
            if (
                self.foreground_answers_for_its_pid
                and front is not None and front.pid == pid
            ):
                return front
            return next(
                (w for w in self.windows.values() if w.pid == pid), None,
            )

        def _process(pid=None):
            window = by_pid(pid)
            if window is None:
                return real_process(pid) if pid is not None else real_process()
            return _FakeProcess(window)

        def _thread_process(hwnd):
            window = self.windows.get(int(hwnd))
            if window is None:
                return (0, 0)
            return (window.hwnd + 1, window.pid)

        stack.enter_context(patch.object(
            win32gui, "GetForegroundWindow", lambda: self.foreground,
        ))
        stack.enter_context(patch.object(
            win32gui, "IsWindow", lambda hwnd: int(hwnd) in self.windows,
        ))
        stack.enter_context(patch.object(
            win32gui, "GetClassName",
            lambda hwnd: self.windows[int(hwnd)].class_name,
        ))
        stack.enter_context(patch.object(
            win32gui, "GetAncestor", lambda hwnd, _flag: int(hwnd),
        ))
        stack.enter_context(patch.object(
            win32process, "GetWindowThreadProcessId", _thread_process,
        ))
        stack.enter_context(patch.object(psutil, "Process", _process))


# ---------------------------------------------------------------------------
# The Win32 hook seam
# ---------------------------------------------------------------------------


class FakeFocusHooks:
    """Stands in for ``OverlayFocusHookManager`` (a Win32 seam class).

    The real class registers ``SetWinEventHook`` hooks on a thread and marshals
    each callback onto the Logic loop. This one keeps the three callbacks the
    real ``start_overlay_focus_hooks`` hands it, so a test can call them the
    way Windows would: ``foreground(hwnd)``, ``menu_popup(event_id)``,
    ``destroyed(hwnd)``.
    """

    instances: list = []

    def __init__(self, loop, on_foreground, on_destroy, on_menu_popup=None):
        self._loop = loop
        self._on_foreground = on_foreground
        self._on_destroy = on_destroy
        self._on_menu_popup = on_menu_popup
        self.started = False
        self.destroy_hook_requests: list = []
        FakeFocusHooks.instances.append(self)

    def start(self) -> bool:
        self.started = True
        return True

    def stop(self) -> None:
        self.started = False

    def is_alive(self) -> bool:
        return self.started

    def register_destroy_hook(self, *, pid: int, tid: int) -> bool:
        self.destroy_hook_requests.append(("register", pid, tid))
        return True

    def unregister_destroy_hook(self) -> bool:
        self.destroy_hook_requests.append(("unregister",))
        return True

    # What Windows does: the hook thread marshals onto the loop.
    def foreground(self, hwnd: int) -> None:
        self._loop.call_soon(self._on_foreground, int(hwnd))

    def menu_popup(self, event_id: int) -> None:
        if self._on_menu_popup is not None:
            self._loop.call_soon(self._on_menu_popup, int(event_id))

    def destroyed(self, hwnd: int) -> None:
        self._loop.call_soon(self._on_destroy, int(hwnd))


class FakeMouse:
    """Stands in for Windows' low-level mouse hook (wh-overlay-refresh-after-mouse).

    Holds a real ``MouseHandler``, connected to Logic the way
    ``ServiceManager.initialize_services`` connects it. Each method calls the
    handler's pynput callbacks on a thread of its own and waits for it, as
    the WH_MOUSE_LL hook thread calls them. ``injected`` is the flag Windows
    sets on an event a program sent (the Input process's SendInput, or a
    WheelHouse scroll command).
    """

    def __init__(self, handler) -> None:
        self.handler = handler
        # When set, a click_snapshot_item request makes the Input process's
        # click send this many injected clicks BEFORE its reply goes back
        # (the SendInput fallback of a number click).
        self.injected_clicks_on_number_click = 0

    def _on_hook_thread(self, calls) -> None:
        import threading

        def _run():
            for callback, args in calls:
                callback(*args)

        thread = threading.Thread(target=_run, name="FakeMouseHook")
        thread.start()
        thread.join(5.0)
        assert not thread.is_alive(), "a mouse callback did not return"

    def click(self, *, injected: bool = False, count: int = 1) -> None:
        """``count`` left clicks: a press and a release each."""

        from pynput.mouse import Button

        calls = []
        for _ in range(count):
            calls.append((self.handler.on_click, (40, 40, Button.left, True, injected)))
            calls.append((self.handler.on_click, (40, 40, Button.left, False, injected)))
        self._on_hook_thread(calls)

    def wheel(self, *, injected: bool = False, steps: int = 1) -> None:
        """``steps`` wheel steps down."""

        self._on_hook_thread(
            [(self.handler.on_scroll, (40, 40, 0, -1, injected))] * steps
        )


# ---------------------------------------------------------------------------
# The clock
# ---------------------------------------------------------------------------


class OffsetClock:
    """Real time plus an offset the test raises.

    No virtual-time library is installed, so the clock is moved by an offset
    added to BOTH clocks main.py reads: the running loop's ``time`` (which
    ``call_later`` and every asyncio timeout use) and ``time.monotonic`` as
    main.py sees it (the focus debounce reads it). ``advance`` raises the
    offset and then lets the loop run, so every timer that came due runs.
    """

    def __init__(self) -> None:
        self.offset = 0.0
        self._loop = None

    def monotonic(self) -> float:
        return _real_time.monotonic() + self.offset

    def install(self, loop, stack: ExitStack) -> None:
        self._loop = loop
        real_loop_time = type(loop).time.__get__(loop)
        loop.time = lambda: real_loop_time() + self.offset

        def _restore():
            try:
                del loop.time
            except AttributeError:
                pass

        stack.callback(_restore)
        stack.enter_context(
            patch.object(main_module, "time", _TimeShim(self)),
        )

    async def advance(self, seconds: float) -> None:
        self.offset += float(seconds)
        await settle()


class _TimeShim:
    """The ``time`` module as main.py sees it, with an offset monotonic."""

    def __init__(self, clock: OffsetClock) -> None:
        self._offset_clock = clock

    def monotonic(self) -> float:
        return self._offset_clock.monotonic()

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


async def settle(rounds: int = 20, real_s: float = 0.02) -> None:
    """Let the loop run every callback that is ready or came due."""

    for _ in range(rounds):
        await asyncio.sleep(0)
    await asyncio.sleep(real_s)
    for _ in range(rounds):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Windows notifications (the user-visible half of an ERROR log record)
# ---------------------------------------------------------------------------


class _RecordingNotifierWorker:
    """Takes the payloads ``ErrorNotificationHandler`` would show as toasts."""

    def __init__(self) -> None:
        self.payloads: list = []

    def submit(self, payload) -> bool:
        self.payloads.append(payload)
        return True


# ---------------------------------------------------------------------------
# Replies built through the production schemas
# ---------------------------------------------------------------------------


def summary(snapshot_id: str, *numbers: int) -> WalkSnapshotSummary:
    """A badge list with one item per display number."""

    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=[
            WalkSnapshotSummaryItem(
                item_id=f"{snapshot_id}-item-{n}",
                display_number=n,
                name=f"control {n}",
                role="Button",
                bounds=(10 * n, 10 * n, 10 * n + 8, 10 * n + 8),
                monitor_id=0,
            )
            for n in numbers
        ],
        created_at_monotonic=1.0,
    )


def item_id(snapshot_id: str, number: int) -> str:
    """The item id ``summary`` gives badge ``number``."""

    return f"{snapshot_id}-item-{number}"


def walk_ok_reply(
    params: dict, *, snapshot_id: str, numbers: Iterable[int] = (1, 2, 3),
    read_from: Window = NUMBERED_WINDOW,
) -> dict:
    """A start_overlay_walk ok reply, through ``StartOverlayWalkResponse``."""

    from services.wheelhouse.shared.start_overlay_walk import (
        StartOverlayWalkResponse,
    )

    return StartOverlayWalkResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=summary(snapshot_id, *numbers),
        trace_id=params["trace_id"],
        overlay_session_id=params["overlay_session_id"],
        paint_generation=params["paint_generation"],
        foreground_window=read_from.hwnd,
        foreground_pid=read_from.pid,
        foreground_process_name=read_from.process_name,
        foreground_window_creation_time=read_from.creation_time_ms,
    ).to_dict()


def walk_failed_reply(params: dict, kind: str) -> dict:
    """A start_overlay_walk reply that reports a failed walk.

    ``kind`` is ``execution_failed`` or ``error`` (the two failure shapes the
    production handler emits, shared/start_overlay_walk.py
    ``_validate_cross_field``), ``no_targets`` (an empty window), or
    ``malformed`` (a dict the schema rejects).
    """

    from services.wheelhouse.shared.start_overlay_walk import (
        StartOverlayWalkResponse,
    )

    if kind == "malformed":
        return {"status": "ok", "outcome": "ok"}
    if kind == "no_targets":
        sid = f"empty-{params['paint_generation']}"
        return StartOverlayWalkResponse(
            status="ok", outcome="no_targets", reason="no_targets",
            snapshot_id=sid, snapshot_summary=summary(sid),
            trace_id=params["trace_id"],
            overlay_session_id=params["overlay_session_id"],
            paint_generation=params["paint_generation"],
        ).to_dict()
    status = "error" if kind == "error" else "ok"
    return StartOverlayWalkResponse(
        status=status, outcome=kind, reason=f"harness_{kind}",
        snapshot_id=None, snapshot_summary=None,
        trace_id=params["trace_id"],
        overlay_session_id=params["overlay_session_id"],
        paint_generation=params["paint_generation"],
    ).to_dict()


def paint_ack(
    state: str, overlay_session_id: int, paint_generation: int,
    snapshot_id: Optional[str] = None,
) -> dict:
    """An overlay_state_changed message, through ``OverlayStateChangedEvent``."""

    from services.wheelhouse.shared.overlay_state_changed import (
        OverlayStateChangedEvent,
    )

    return OverlayStateChangedEvent(
        state=state,
        overlay_session_id=overlay_session_id,
        paint_generation=paint_generation,
        monitor_ids=(0,),
        snapshot_id=snapshot_id,
    ).to_dict()


def _click_answer(params: dict) -> dict:
    from services.wheelhouse.shared.click_element import ClickElementResponse

    return ClickElementResponse(
        status="ok",
        outcome="ok",
        reason=None,
        matched_names=("control",),
        snapshot_id=params.get("snapshot_id"),
        snapshot_summary=None,
        matched_name="control",
        trace_id=params.get("trace_id", ""),
    ).to_dict()


def _pin_answer(action: str, params: dict) -> dict:
    from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=params.get("overlay_session_id", 0),
        snapshot_id=params.get("snapshot_id", ""),
        pinned=(action == "pin_snapshot"),
    ).to_dict()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace('"', '\\"') + '"'
    return repr(value)


def write_config(tmp_path: Path, name: str = "harness-config.toml", **click) -> Path:
    """Copy the tracked config.toml.example with changed ``[click]`` keys.

    The copy keeps every other section (the speech handler needs its timeout
    keys). A key already in the ``[click]`` block has its line replaced; a new
    key is added at the top of the block.
    """

    lines = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    start = lines.index("[click]")
    end = next(
        (i for i in range(start + 1, len(lines))
         if re.match(r"^\[[^\]]+\]\s*$", lines[i])),
        len(lines),
    )
    pending = dict(click)
    for i in range(start + 1, end):
        m = re.match(r"^([A-Za-z0-9_]+)\s*=", lines[i])
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)} = {_toml_value(pending.pop(m.group(1)))}"
    for key, value in pending.items():
        lines.insert(start + 1, f"{key} = {_toml_value(value)}")
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


@dataclass
class Frame:
    """One command Logic wrote for the Input process."""

    index: int
    action: str
    params: dict
    request_id: Any
    trace_id: Any


@dataclass
class OverlayBoundary:
    """Inputs to and outputs from one real Logic process, under test control."""

    loop: Any
    app: WheelHouseApp
    controller: LogicController
    shm: shared_memory.SharedMemory
    command_ready_event: Any
    state_to_gui_queue: Any
    gui_to_logic_queue: Any
    desktop: FakeDesktop
    clock: OffsetClock
    speech_processor: Any
    notifier: _RecordingNotifierWorker
    mouse: FakeMouse
    frames: list = field(default_factory=list)
    gui: list = field(default_factory=list)
    _consumed_frames: set = field(default_factory=set)
    _consumed_gui: set = field(default_factory=set)
    _gui_barriers: dict = field(default_factory=dict)
    _utterance_id: int = 0
    _tasks: list = field(default_factory=list)

    # -- the Win32 hooks -----------------------------------------------------
    @property
    def hooks(self) -> FakeFocusHooks:
        assert FakeFocusHooks.instances, (
            "the controller started no focus hooks (the overlay is off in "
            "this config, so no Win32 callback reaches Logic)"
        )
        return FakeFocusHooks.instances[-1]

    @property
    def notifications(self) -> list:
        return list(self.notifier.payloads)

    # -- speech ------------------------------------------------------------
    async def say(self, text: str) -> None:
        """Speak one utterance: its words, then the end-of-utterance marker."""

        from speech.word_event import WordEvent

        self._utterance_id += 1
        words = text.split()
        for index, word in enumerate(words):
            await self.speech_processor.process_word_event(
                WordEvent(
                    word=word,
                    start_of_utterance=(index == 0),
                    end_of_utterance=(index == len(words) - 1),
                    utterance_id=self._utterance_id,
                )
            )
        await self.speech_processor.process_word_event(
            WordEvent(
                word="",
                start_of_utterance=False,
                end_of_utterance=True,
                utterance_id=self._utterance_id,
                is_utterance_end_marker=True,
            )
        )
        await settle()

    def say_soon(self, text: str) -> asyncio.Task:
        """Speak in a task, for an utterance whose handling awaits a reply."""

        task = asyncio.ensure_future(self.say(text))
        self._tasks.append(task)
        return task

    # -- the Input side ------------------------------------------------------
    def mark(self) -> tuple[int, int]:
        """A position in both output logs, for ``since``."""

        return (len(self.frames), len(self.gui))

    def frames_since(self, mark, actions=OVERLAY_INPUT_ACTIONS) -> list:
        return [f for f in self.frames[mark[0]:] if f.action in actions]

    def gui_since(self, mark, actions=OVERLAY_GUI_ACTIONS) -> list:
        return [
            m for m in self.gui[mark[1]:]
            if isinstance(m, dict) and m.get("action") in actions
        ]

    async def next_frame(
        self, action: str, *, timeout_s: float = 5.0,
        where: Optional[Callable[[dict], bool]] = None,
    ) -> Frame:
        """The oldest frame for ``action`` no earlier call has returned.

        ``where``, when given, must also accept the frame's params.
        """

        deadline = _real_time.monotonic() + timeout_s
        while True:
            for frame in self.frames:
                if (
                    frame.action == action
                    and frame.index not in self._consumed_frames
                    and (where is None or where(frame.params))
                ):
                    self._consumed_frames.add(frame.index)
                    return frame
            if _real_time.monotonic() > deadline:
                raise AssertionError(
                    f"Logic sent no {action!r} to Input within {timeout_s}s; "
                    f"frames seen: {[f.action for f in self.frames]}"
                )
            await asyncio.sleep(0.005)

    def reply(self, frame: Frame, payload: dict) -> None:
        """Answer one request the way the Input process does."""

        assert frame.request_id is not None, (
            f"{frame.action} went out fire-and-forget; nothing can answer it"
        )
        message = dict(payload)
        message["request_id"] = frame.request_id
        message["action"] = frame.action
        self.app.response_queue.put(message)

    async def answer_walk(self, payload_for: Callable[[dict], dict]) -> Frame:
        """Wait for the next start_overlay_walk and answer it."""

        frame = await self.next_frame("start_overlay_walk")
        self.reply(frame, payload_for(frame.params))
        await settle()
        return frame

    # -- the GUI side ------------------------------------------------------
    def send_gui(self, message: dict) -> None:
        """A message from the GUI process to Logic."""

        self.gui_to_logic_queue.put(message)

    async def send_gui_as_time_passes(self, message: dict, seconds: float) -> None:
        """A message from the GUI process, with ``seconds`` passing as Logic reads it.

        Bead wh-overlay-timer-cancel-race. Only the queue and the clock are
        touched: the message goes on the real GUI-to-Logic queue, and the
        clock moves forward by ``seconds`` at the moment the GUI command
        listener first reads the message on the Logic loop (main.py
        ``listen_for_gui_commands`` reads its ``action`` first), not before.

        LOOP ORDER THIS PRODUCES. Call the loop turn in which the listener
        reads the message turn N. In turn N the listener starts its handler
        for the message (``overlay_state_changed`` runs as a task), so the
        handler's first step is on the ready queue for turn N+1. At the start
        of turn N+1 asyncio moves every timer that the moved clock made due
        onto the ready queue BEHIND that step. So in turn N+1 the handler's
        first step runs first and the due timer callback runs after it, in
        the same turn, before any task that the handler's step started. That
        is the position of a timer callback that is already on the ready
        queue when a transition commits: the transition has committed, and
        its CANCEL_TIMER effect, which runs in a task, has not run.

        Waits until the listener has handled the message (``settle_gui``).
        """

        clock = self.clock

        def _move_the_clock() -> None:
            clock.offset += float(seconds)

        self.gui_to_logic_queue.put_with_first_read(message, _move_the_clock)
        await self.settle_gui()

    async def next_gui(self, action: str, *, timeout_s: float = 5.0) -> dict:
        """The oldest GUI message for ``action`` no earlier call has returned."""

        deadline = _real_time.monotonic() + timeout_s
        while True:
            for index, message in enumerate(self.gui):
                if (
                    isinstance(message, dict)
                    and message.get("action") == action
                    and index not in self._consumed_gui
                ):
                    self._consumed_gui.add(index)
                    return message
            if _real_time.monotonic() > deadline:
                raise AssertionError(
                    f"Logic sent no {action!r} to the GUI within {timeout_s}s; "
                    f"messages seen: "
                    f"{[m.get('action') for m in self.gui if isinstance(m, dict)]}"
                )
            await asyncio.sleep(0.005)

    async def gui_ack(self, state: str, paint: dict) -> None:
        """The GUI acknowledges ``paint`` (a paint_overlay message)."""

        self.send_gui(paint_ack(
            state, paint["overlay_session_id"], paint["paint_generation"],
            paint.get("snapshot_id"),
        ))
        await self.settle_gui()

    async def settle_gui(self, *, timeout_s: float = 5.0) -> None:
        """Wait until the GUI command listener has handled every sent message.

        The listener reads the queue on a worker thread (main.py
        ``listen_for_gui_commands``, ``asyncio.to_thread``) and handles each
        message on the loop before it asks the queue for the next one. So
        when it asks again after taking every message sent so far, it has
        handled them all; ``settle`` then runs the tasks they started.
        """

        queue = self.gui_to_logic_queue
        deadline = _real_time.monotonic() + timeout_s
        while queue.handled < queue.sent:
            if _real_time.monotonic() > deadline:
                raise AssertionError(
                    f"the GUI command listener handled {queue.handled} of "
                    f"{queue.sent} messages within {timeout_s}s"
                )
            await asyncio.sleep(0.001)
        await settle()

    # -- flushing and quiet windows ----------------------------------------
    async def flush(self) -> None:
        """Wait until every message Logic has already sent has been collected.

        Two FIFO barriers: a request through the real sender (so every frame
        queued before it has been written and read), and a marker on the GUI
        queue (so every dict put before it has crossed the feeder thread).
        """

        reply = await self.app.send_request(_INPUT_BARRIER, {}, timeout_s=5.0)
        assert reply.get("status") == "ok", f"input barrier failed: {reply!r}"
        token = f"_overlay_harness_gui_barrier_{uuid.uuid4().hex}"
        done = asyncio.Event()
        self._gui_barriers[token] = done
        self.state_to_gui_queue.put((token,))
        await asyncio.wait_for(done.wait(), 5.0)

    async def quiet(self, since, window_s: float = 0.3) -> tuple[list, list]:
        """Overlay outputs from ``since`` (a ``mark``) to ``window_s`` from now.

        Returns ``(input_frames, gui_messages)`` restricted to the overlay
        action sets. A test that expects nothing asserts both are empty. The
        mark is the caller's, taken BEFORE the input under test, so output
        the input caused while it was being delivered is counted too.
        """

        mark = since
        await asyncio.sleep(window_s)
        await settle()
        await self.flush()
        return self.frames_since(mark), self.gui_since(mark)

    async def advance(self, seconds: float) -> None:
        """Let ``seconds`` of time pass for every Logic timer."""

        await self.clock.advance(seconds)
        await settle()

    # -- common setup ----------------------------------------------------------
    async def show_numbers_painted(
        self, snapshot_id: str = "snap-1", numbers: Iterable[int] = (1, 2, 3),
        *, read_from: Window = NUMBERED_WINDOW,
    ) -> dict:
        """'show numbers', the walk answered, the paint acknowledged.

        Returns the paint_overlay message the GUI was sent. Waits for the
        first lease renew, which Logic sends only once the overlay is painted
        (main.py ``_fire_overlay_keepalive``), so the caller starts from a
        painted overlay it saw at the boundary.
        """

        numbers = tuple(numbers)
        await self.say("show numbers")
        await self.answer_walk(lambda p: walk_ok_reply(
            p, snapshot_id=snapshot_id, numbers=numbers, read_from=read_from,
        ))
        paint = await self.next_gui("paint_overlay")
        await self.gui_ack("painted", paint)
        await self.next_gui("overlay_lease_renew")
        await self.flush()
        return paint

    # -- collectors ------------------------------------------------------------
    async def _collect_input(self) -> None:
        while True:
            if self.command_ready_event.is_set():
                action, params, _has, request_id, trace_id = _read_frame(self.shm)
                # Copy-then-clear, as the Input loop does (input_proc.py:1066).
                self.command_ready_event.clear()
                if action == _INPUT_BARRIER:
                    self.app.response_queue.put({
                        "status": "ok", "action": action,
                        "request_id": request_id,
                    })
                    continue
                frame = Frame(
                    index=len(self.frames), action=action,
                    params=dict(params or {}), request_id=request_id,
                    trace_id=trace_id,
                )
                self.frames.append(frame)
                if action in _AUTO_ANSWERED and request_id is not None:
                    self.reply(frame, _pin_answer(action, frame.params))
                elif action in _AUTO_CLICKED and request_id is not None:
                    if action == "click_snapshot_item":
                        injected = self.mouse.injected_clicks_on_number_click
                        if injected:
                            self.mouse.click(injected=True, count=injected)
                            await settle()
                    self.reply(frame, _click_answer(frame.params))
            await asyncio.sleep(0.001)

    async def _collect_gui(self) -> None:
        while True:
            try:
                message = self.state_to_gui_queue.get_nowait()
            except queue_mod.Empty:
                await asyncio.sleep(0.002)
                continue
            if (
                isinstance(message, tuple) and len(message) == 1
                and message[0] in self._gui_barriers
            ):
                self._gui_barriers.pop(message[0]).set()
                continue
            self.gui.append(message)


class _ListenerQueue:
    """The real GUI-to-Logic multiprocessing.Queue, plus two counts.

    ``sent`` counts the messages put on the queue. ``handled`` is the number
    of messages the listener had taken when it last asked the queue for
    another one; the listener handles each message before it asks again, so
    every one of those messages is handled. ``OverlayBoundary.settle_gui``
    waits on the two counts.
    """

    def __init__(self, queue) -> None:
        self._queue = queue
        self._lock = threading.Lock()
        self.sent = 0
        self.taken = 0
        self.handled = 0
        # Queue position -> the callable to run when Logic first reads the
        # message at that position (``OverlayBoundary.send_gui_as_time_passes``).
        self._on_first_read: dict = {}

    def put(self, message, *args, **kwargs) -> None:
        self.put_with_first_read(message, None, *args, **kwargs)

    def put_with_first_read(self, message, on_first_read, *args, **kwargs) -> None:
        with self._lock:
            if on_first_read is not None:
                self._on_first_read[self.sent] = on_first_read
            self.sent += 1
        self._queue.put(message, *args, **kwargs)

    def get(self, *args, **kwargs):
        with self._lock:
            self.handled = self.taken
        message = self._queue.get(*args, **kwargs)
        with self._lock:
            on_first_read = self._on_first_read.pop(self.taken, None)
            self.taken += 1
        if on_first_read is not None:
            message = _RunsOnFirstRead(message, on_first_read)
        return message

    def __getattr__(self, name: str):
        return getattr(self._queue, name)


class _RunsOnFirstRead(dict):
    """A GUI message, as a dict, that runs one callable when it is first read.

    ``_ListenerQueue.get`` builds it on the listener's worker thread and does
    not read it there, so the first read is the listener's own, on the Logic
    loop. Any later read sees the plain message.
    """

    def __init__(self, message: dict, on_first_read: Callable[[], None]) -> None:
        super().__init__(message)
        self._pending = on_first_read

    def _first_read(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            pending()

    def get(self, *args):
        self._first_read()
        return super().get(*args)

    def __getitem__(self, key):
        self._first_read()
        return super().__getitem__(key)

    def __contains__(self, key) -> bool:
        self._first_read()
        return super().__contains__(key)


def _read_frame(shm: shared_memory.SharedMemory) -> tuple:
    """De-frame one Logic-to-Input command the production way.

    Same three statements as the Input command loop (input_proc.py:1059-1068),
    then production's own ``input_proc._read_envelope``. Copied from
    tests/test_overlay_journeys.py ``_read_command_frame``.
    """

    msg_len = struct.unpack(">I", bytes(shm.buf[:4]))[0]
    msg_data = bytearray(msg_len)
    msg_data[:] = shm.buf[4:4 + msg_len]
    extracted = input_proc._read_envelope(pickle.loads(msg_data))
    assert extracted is not None, (
        "production's _read_envelope rejected the frame Logic wrote"
    )
    return extracted


@asynccontextmanager
async def overlay_boundary(
    tmp_path: Path, *, config_path: Optional[Path] = None,
    windows: Iterable[Window] = (NUMBERED_WINDOW, OTHER_WINDOW),
):
    """Start one real Logic process's overlay pieces and yield its boundary."""

    loop = asyncio.get_running_loop()
    data_dir = tmp_path / "user-data"
    data_dir.mkdir(exist_ok=True)
    FakeFocusHooks.instances = []
    desktop = FakeDesktop(windows)
    clock = OffsetClock()
    notifier = _RecordingNotifierWorker()

    from utils.error_notifier import ErrorNotificationHandler

    error_handler = ErrorNotificationHandler(notifier_worker=notifier)
    root_logger = logging.getLogger()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(utils_system, "get_user_data_dir", lambda: data_dir)
        )
        stack.enter_context(
            patch.object(
                overlay_controller_module, "OverlayFocusHookManager",
                FakeFocusHooks,
            )
        )
        desktop.install(stack)
        clock.install(loop, stack)
        root_logger.addHandler(error_handler)
        stack.callback(root_logger.removeHandler, error_handler)

        config_service = ConfigService(
            str(config_path if config_path is not None else EXAMPLE_CONFIG_PATH)
        )
        event_bus = EventBus()
        shutdown_event = MpEvent()
        state_to_gui_queue = MpQueue()
        gui_to_logic_queue = _ListenerQueue(MpQueue())
        state_manager = StateManager(
            config_service=config_service,
            event_bus=event_bus,
            loop=loop,
            state_to_gui_queue=state_to_gui_queue,
            websocket_manager=None,
        )
        shm = shared_memory.SharedMemory(create=True, size=1024 * 64)
        command_ready_event = MpEvent()
        controller = None
        app = None
        boundary = None
        try:
            app = WheelHouseApp(
                shm_name=shm.name,
                command_ready_event=command_ready_event,
                ui_ready_event=MpEvent(),
                response_queue=MpQueue(),
            )
            service_manager = ServiceManager(
                config_service=config_service,
                event_bus=event_bus,
                loop=loop,
                app=app,
                state_manager=state_manager,
            )
            controller = LogicController(
                app=app,
                config_service=config_service,
                shutdown_event=shutdown_event,
                event_bus=event_bus,
                service_manager=service_manager,
                state_manager=state_manager,
                gui_shm_name=None,
            )
            from speech.speech_handler import SpeechHandler

            speech_handler = SpeechHandler(app, controller, config_service)
            assert speech_handler.pattern_catalog.pattern_count > 0, (
                "the pattern catalog loaded in degraded mode, so every "
                "utterance would dictate"
            )
            speech_handler.initialize_speech_processor(asyncio.Queue())
            # main() wires the Logic controller onto the service manager
            # before initialize_services, which connects the mouse handler.
            service_manager.set_logic_controller(controller)
            from services.wheelhouse.handlers.mouse_handler import MouseHandler

            mouse_handler = MouseHandler(
                loop, config_service, app, None, None, None, event_bus,
            )
            mouse_handler.set_pointer_input_listener(
                service_manager.overlay_pointer_input_listener()
            )
            # main() wires the speech handler onto the service manager
            # (initialize_services); the overlay's sentence-end wait reads it
            # there.
            service_manager.speech_handler = speech_handler

            app.start_demuxer()
            app.start_sender()

            boundary = OverlayBoundary(
                loop=loop,
                app=app,
                controller=controller,
                shm=shm,
                command_ready_event=command_ready_event,
                state_to_gui_queue=state_to_gui_queue,
                gui_to_logic_queue=gui_to_logic_queue,
                desktop=desktop,
                clock=clock,
                speech_processor=speech_handler.speech_processor,
                notifier=notifier,
                mouse=FakeMouse(mouse_handler),
            )
            boundary._tasks.append(asyncio.ensure_future(boundary._collect_input()))
            boundary._tasks.append(asyncio.ensure_future(boundary._collect_gui()))

            # What LogicController.main() starts for the overlay, in its order.
            controller.overlay.start_overlay_focus_hooks()   # main.py LogicController.main
            controller.overlay.send_overlay_startup_reset()  # main.py LogicController.main
            controller.create_task_with_error_handling(      # main.py LogicController.main
                controller.listen_for_gui_commands(gui_to_logic_queue),
                "GuiCommandListener",
            )
            await settle()
            yield boundary
        finally:
            shutdown_event.set()
            try:
                gui_to_logic_queue.put({"action": "_overlay_harness_stop"})
            except Exception:  # noqa: BLE001 -- teardown is best-effort
                pass
            if boundary is not None:
                for task in boundary._tasks:
                    task.cancel()
                await asyncio.gather(*boundary._tasks, return_exceptions=True)
            if controller is not None:
                pending = [t for t in controller.background_tasks if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            if app is not None:
                await app.stop()
                app.shm.close()
            shm.close()
            shm.unlink()
            state_to_gui_queue.close()
            gui_to_logic_queue.close()
