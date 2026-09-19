"""Cross-process voice journeys around the numbered overlay.

Bead: wh-overlay-slow-uia-stale-badges.12.

WHY THIS FILE EXISTS. Every failure the parent bug found crosses a process
boundary or a timing boundary, and a unit test inside one process cannot show
any of them. The existing overlay tests are all one-sided: they assert the
Logic-side effect as data, or the Input-side handler in isolation, or the GUI
consumer with a fake queue. None of them carries an utterance from the voice
entry point through the real IPC framing and back.

THE METHOD, recorded in full as a bd comment on the bead. In short: a fake
accessibility provider drives REAL production code over REAL transport
objects inside ONE test process, with every clock the sequence depends on
under the test's control.

  * No journey drives a real application window. The one live-tree test in
    this suite (tests/test_uia_walker.py:1005) is skipped for needing a live
    desktop and an STA COM apartment, and the bead requires these to run in a
    normal test run.

  * No journey spawns a WheelHouse process. The Logic-to-Input frames are
    written with the PRODUCTION framing (app.py _frame_and_write) into a real
    ``shared_memory.SharedMemory`` and read back with the production envelope
    reader, and the three queues are real ``multiprocessing.Queue`` objects.
    Real queues rather than list doubles on purpose: a list proves the payload
    was handed over, a real queue proves it can be PICKLED, which is what the
    shipped system actually requires (the start method is spawn).

  * The accessibility read is injected, not mocked at the COM level.
    ``ElementFinder.__init__`` takes ``walk_fn``, ``popup_walk_fn``,
    ``taskbar_walk_fn`` and ``clock`` as keyword-only arguments, and
    ``UIActionHandler._get_click_element_finder`` returns a pre-set
    ``_click_element_finder`` before it builds anything, so no COM object is
    ever created.

  * Time is injected, never slept, except for one deliberate 1.0 s host stall
    in ``test_grid_say_waits_for_processing_after_collection_clock_expires``.
    The badge lease is 90 s (gui.py _OVERLAY_LEASE_DEFAULT_MS) against a 30 s per-test cap
    (pyproject.toml timeout), so real time is not available to these tests at
    all.

EXPECTED FAILURES. A journey whose fix is not merged carries
``@pytest.mark.xfail(strict=True, ...)`` naming the blocking bead, following
the one existing precedent in this suite
(tests/test_router_command_prefix_word_loss.py:115). Strict on purpose: the
marker itself fails the suite on the day the blocking fix lands, which is the
signal to delete the marker and let the journey stand on its own.
"""

from __future__ import annotations

import asyncio
import pickle
import queue as queue_mod
import struct
import time
from contextlib import ExitStack, asynccontextmanager, contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from multiprocessing import Event as MpEvent
from multiprocessing import Queue as MpQueue
from multiprocessing import shared_memory
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ``input_proc`` and ``utils.system`` are the two TOP-LEVEL modules this file
# reaches into. Both are imported by module name, not through the
# ``services.wheelhouse`` package, and that distinction is load-bearing for
# the hermeticity patch below -- see ``_real_logic_controller``.
import input_proc
import utils.system as utils_system
from services.wheelhouse.app import WheelHouseApp
from services.wheelhouse.click_overlay_state import (
    OverlayEvent,
    OverlayEventKind,
    OverlayState,
    PaintAckState,
)
from services.wheelhouse.config_service import ConfigService
from services.wheelhouse.event_bus import EventBus
from services.wheelhouse.main import LogicController
from services.wheelhouse.overlay_focus_hooks import ForegroundIdentity
from services.wheelhouse.service_manager import ServiceManager
from services.wheelhouse.state_manager import StateManager
from services.wheelhouse.ui.element_types import (
    ElementQuery,
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)

pytestmark = pytest.mark.journey

# The service configuration every journey reads. It is deliberately NOT
# ``services/wheelhouse/config.toml``. That file IS tracked -- measured with
# ``git ls-files --error-unmatch`` and ``git check-ignore -v`` on 2026-08-29 --
# but it is also the file a user edits, so its working-tree content is machine
# state rather than repository state. A journey that read it would change
# behaviour the day the machine's owner flips a legitimate local setting.
# ``config.toml.example`` is tracked, is the file the repository ships as the
# starting point, and carries the same ``[click]`` block a clean checkout gets.
_SERVICE_DIR = Path(__file__).resolve().parent.parent
_JOURNEY_CONFIG_PATH = _SERVICE_DIR / "config.toml.example"


# ---------------------------------------------------------------------------
# The real-controller rig
# ---------------------------------------------------------------------------


@dataclass
class JourneyRig:
    """Everything a journey needs to drive Logic and read the Input side.

    ``controller`` is a REAL ``LogicController`` built through its real
    ``__init__`` -- not the ``object.__new__`` short cut the existing overlay
    tests use. That short cut hand-injects about 28 attributes
    (tests/test_logic_overlay_integration.py:116); the list can drift out of
    step with the real constructor, and a journey built on it would keep
    passing while real construction broke.

    ``shm`` is the CREATOR handle for the Logic-to-Input buffer. The test
    reads frames through it, standing in for the Input process's own
    attachment; ``app.shm`` is the sender's separate attachment to the same
    named block.
    """

    controller: LogicController
    app: WheelHouseApp
    shm: shared_memory.SharedMemory
    command_ready_event: Any
    state_to_gui_queue: Any
    data_dir: Path


@asynccontextmanager
async def _real_logic_controller(
    data_dir: Path, *, config_path: Path | None = None,
):
    """Build a real ``LogicController`` over real transport objects.

    HERMETICITY (the reason for the patch below). ``LogicController.__init__``
    reads two real files off this machine's disk:

      * main.py:1296-1303 -- ``_resolve_pending_counters_path()`` then
        ``ClickCounter(...).load_from_disk()``.
      * main.py:1321 -- ``_load_declined_tuples()``.

    Both resolve through ``utils/system.py:34 get_user_data_dir()``, which in
    a source checkout returns ``services/wheelhouse/data`` (utils/system.py:52)
    -- a directory holding this machine's accumulated approved-control and
    declined-control state. Without the patch a journey would load it and then
    behave differently on another machine and after ordinary use of the
    application.

    The patch target is the ``utils.system`` MODULE ATTRIBUTE, and it must be
    exactly that module object. Both resolvers do a function-local
    ``from utils.system import get_user_data_dir`` (main.py:10702 and
    main.py:10688), so the name is looked up on the module at CALL time, which
    is what makes patching work at all. ``utils.system`` and
    ``services.wheelhouse.utils.system`` are two DISTINCT module objects in
    this suite (the conftest puts both the repo root and the service directory
    on ``sys.path``); measured 2026-08-29, patching the package-qualified one
    leaves the resolvers reading the real directory, so only the top-level
    module is patched here.

    Both resolvers also honour an instance override (``self._declined_path``),
    but that is unusable here: the reads happen INSIDE ``__init__``, before a
    test can set any attribute.

    A THIRD machine-specific source reaches this constructor: the service
    configuration. ``ConfigService()`` with no argument resolves to
    ``services/wheelhouse/config.toml`` (config_service.py:44-51), the file a
    user edits, so the journeys pass ``_JOURNEY_CONFIG_PATH`` instead and read
    the tracked example. Proved by the three ``test_config_part_*`` tests.
    """

    loop = asyncio.get_running_loop()
    with patch.object(
        utils_system, "get_user_data_dir", lambda: data_dir,
    ):
        config_service = ConfigService(
            str(config_path if config_path is not None else _JOURNEY_CONFIG_PATH)
        )
        event_bus = EventBus()
        shutdown_event = MpEvent()
        state_to_gui_queue = MpQueue()
        state_manager = StateManager(
            config_service=config_service,
            event_bus=event_bus,
            loop=loop,
            state_to_gui_queue=state_to_gui_queue,
            websocket_manager=None,
        )
        # WheelHouseApp.__init__ ATTACHES (app.py:182 passes no create=True),
        # so the block must exist first. Named segments outlive the process
        # that made them, so close() + unlink() below are mandatory.
        shm = shared_memory.SharedMemory(create=True, size=1024 * 64)
        command_ready_event = MpEvent()
        controller = None
        app = None
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
            # The two IPC background tasks by themselves, NOT app.start():
            # start() also builds a WebSocketManager and (optionally) opens a
            # real socket, neither of which any journey uses.
            app._start_demuxer()
            app._start_sender()
            yield JourneyRig(
                controller=controller,
                app=app,
                shm=shm,
                command_ready_event=command_ready_event,
                state_to_gui_queue=state_to_gui_queue,
                data_dir=data_dir,
            )
        finally:
            if controller is not None:
                pending = [
                    t for t in controller.background_tasks if not t.done()
                ]
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


@pytest.fixture
async def journey_rig(tmp_path):
    """A real LogicController whose user-data directory is a pytest tmp_path."""

    data_dir = tmp_path / "user-data"
    data_dir.mkdir()
    async with _real_logic_controller(data_dir) as rig:
        # A journey that silently ran with clicking switched off would assert
        # nothing. The rig reads the TRACKED config.toml.example, so these
        # three hold on every checkout and can only fire if that tracked file
        # changes -- which is the point: the day someone edits the shipped
        # [click] block in a way these journeys cannot run under, they say so
        # by name instead of failing somewhere unrecognisable.
        assert rig.controller.click_config.enabled, (
            f"[click] enabled is False in {_JOURNEY_CONFIG_PATH.name} "
            f"(invalid_key={rig.controller.click_config.invalid_key!r}); "
            "the overlay journeys cannot run"
        )
        assert rig.controller.click_config.overlay_enabled_effective
        # Never enable overlay_settle_after_click here: it ships off by the
        # repository owner's decision (ui/click_config.py:223 and :714).
        assert not rig.controller.click_config.overlay_settle_after_click
        yield rig


# ---------------------------------------------------------------------------
# Reading the Input side of the boundary
# ---------------------------------------------------------------------------


def _read_command_frame(shm: shared_memory.SharedMemory) -> tuple:
    """De-frame one Logic-to-Input command the production way.

    THE WRITE SIDE IS NOT REIMPLEMENTED. The frame under the cursor was
    produced by the real sender chain -- ``WheelHouseApp.send_request``
    (app.py:1082) -> ``_sender_loop`` (:878) -> ``_send_one`` (:675) ->
    ``_frame_and_write`` (:596). tests/test_ipc_fault_injection.py:28-47
    copies that framing into a local ``_write_frame`` and says so in its own
    docstring; a copy cannot catch a change to production framing, which is
    the exact class of defect a journey test exists to catch.

    THE READ SIDE reuses production wherever production offers something to
    call. The envelope extraction runs through ``input_proc._read_envelope``
    (input_proc.py:534), the same chokepoint the Input loop uses at
    input_proc.py:1110 and the same one tests/e2e/app_adapter.py:622 reuses.

    The three lines that turn bytes into a dict are the one part with no
    callable production counterpart: they live INLINE in the Input command
    loop (input_proc.py:1059-1068), inside ``input_process_main``, which a
    test cannot call. That entry point reconfigures logging (:889), raises the
    calling process's scheduling class (:896), replaces the interpreter fault
    handler (:905), and installs real system-wide pynput mouse and keyboard
    listeners (:1021-1022) -- all against whatever process calls it. So the
    header read and the unpickle are written out here, deliberately kept to
    the same three statements as production, and nothing else is.

    Returns ``_read_envelope``'s tuple:
    ``(action, raw_params, has_params, request_id, trace_id)``.
    """

    size_bytes = bytes(shm.buf[:4])
    msg_len = struct.unpack(">I", size_bytes)[0]
    msg_data = bytearray(msg_len)
    msg_data[:] = shm.buf[4:4 + msg_len]
    command_message = pickle.loads(msg_data)

    extracted = input_proc._read_envelope(command_message)
    assert extracted is not None, (
        "production's _read_envelope rejected the frame Logic wrote"
    )
    return extracted


async def _await_frame(command_ready_event, *, timeout_s: float = 3.0) -> None:
    """Wait until the Logic sender signals a frame, the way Input polls."""

    deadline = time.monotonic() + timeout_s
    while not command_ready_event.is_set():
        if time.monotonic() > deadline:
            raise AssertionError(
                f"no IPC frame was signalled within {timeout_s}s"
            )
        await asyncio.sleep(0.005)


async def _assert_no_further_frame(
    rig: "JourneyRig", *, window_s: float, why: str,
) -> None:
    """Assert the command frame stays free for ``window_s``.

    The caller must have CLEARED the event first, exactly as the Input loop
    does after copying a frame (input_proc.py:1066). Without that clear this
    proves nothing: ``_send_one`` refuses to overwrite an unread frame and
    drops the payload as ``unread_frame`` (app.py:764-773), so a second
    command would be absent for a transport reason instead of the Logic-side
    reason under test.

    A clear alone is not always enough, either. ``_send_one`` PARKS a payload
    it cannot deliver on that same event for up to ``_COMMAND_DELIVERY_TTL_S``
    (app.py:753-774, 5.0s), so a frame the caller's own clear releases lands
    inside the window and trips this assertion. When the caller reached its
    state through the real integration rather than through direct machine
    ``apply`` calls, use ``_drain_command_frames`` instead of a bare clear.

    On failure this names the action that actually arrived. The event is only
    a flag, so the caller's ``why`` can only ever describe the frame it
    EXPECTED to be absent; without the action, a parked bookkeeping frame and
    the command under test are indistinguishable in the output. Reading the
    frame is safe here: the failure ends the test, so nothing later depends
    on the buffer being untouched.
    """

    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if rig.command_ready_event.is_set():
            try:
                arrived = _read_command_frame(rig.shm)[0]
            except Exception as e:  # pragma: no cover - diagnosis only
                arrived = f"<unreadable: {e}>"
            raise AssertionError(f"{why} (the frame that arrived: {arrived})")
        await asyncio.sleep(0.01)


async def _drain_gui_queue(queue, *, settle_s: float = 0.3) -> list:
    """Collect every action put on a REAL multiprocessing state queue.

    A multiprocessing.Queue hands items to a feeder thread, so an item put by
    ``_forward_click_notice`` is not readable the instant ``put_nowait``
    returns; poll for a bounded window rather than reading once.
    """

    drained: list = []
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        try:
            drained.append(queue.get_nowait())
        except queue_mod.Empty:
            await asyncio.sleep(0.01)
    return drained


# ---------------------------------------------------------------------------
# Overlay state setup
# ---------------------------------------------------------------------------


def _summary(snapshot_id: str, *display_numbers: int) -> WalkSnapshotSummary:
    """A painted badge list. Same shape the existing overlay tests build."""

    return WalkSnapshotSummary(
        snapshot_id=snapshot_id,
        items=[
            WalkSnapshotSummaryItem(
                item_id=f"{snapshot_id}-item-{n}",
                display_number=n,
                name=f"control {n}",
                role="Button",
                bounds=(0, 0, 10, 10),
                monitor_id=0,
            )
            for n in display_numbers
        ],
        created_at_monotonic=1.0,
    )


def _paint_badges(
    controller: LogicController, snapshot_id: str, *display_numbers: int,
) -> tuple[int, int]:
    """Put the controller's OWN overlay machine into PAINTED with these badges.

    Drives the real ``ClickOverlayStateMachine`` through its real transitions
    (closed -> walk_in_flight -> paint_in_flight -> painted) with direct
    ``apply`` calls, the same pure setup
    tests/test_logic_overlay_integration.py:2003 ``_drive_to_painted`` and
    tests/test_voice_overlay_routing.py use. No machine or cache is injected:
    the ones built by the real ``__init__`` (main.py:1387 and :1419) are the
    ones used, so the trap those fixtures document -- an empty
    ``ClickSnapshotSummaryCache`` is FALSY, so ``cache or
    ClickSnapshotSummaryCache()`` silently discards an injected empty cache --
    cannot arise here.

    Returns the (overlay_session_id, paint_generation) pair now on screen.
    """

    machine = controller.click_overlay_state
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    session_id, generation = machine.overlay_session_id, machine.paint_generation
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=session_id,
            paint_generation=generation,
            snapshot_id=snapshot_id,
        )
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=session_id,
            paint_generation=generation,
            paint_state=PaintAckState.PAINTED,
        )
    )
    assert machine.state is OverlayState.PAINTED
    controller.click_snapshot_summary_cache.put(
        snapshot_id, _summary(snapshot_id, *display_numbers),
    )
    # Production bookkeeping, called rather than imitated: this is what makes
    # a dispatched click carry overlay_resolved_pair (main.py:8237, read at
    # main.py:3579).
    controller._reconcile_overlay_visible_painted_pair()
    assert controller._overlay_visible_painted_pair == (session_id, generation)
    return session_id, generation


def _speak_click(number_word: str) -> ElementQuery:
    """The ElementQuery ClickCommandParser produces for "click <word>"."""

    return ElementQuery(
        name=number_word,
        role=None,
        ordinal=None,
        spatial=None,
        raw_utterance=f"click {number_word}",
    )


# ---------------------------------------------------------------------------
# Deliverable B: the isolation is real, and it is what makes the rig hermetic
# ---------------------------------------------------------------------------


async def test_controller_reads_the_injected_data_dir_not_this_machine(tmp_path, monkeypatch):
    """The rig's user-data isolation loads test files and never machine state.

    A nonempty decoy replaces the machine's data directory. The seeded
    rig must load it, and the empty rig must override it and load nothing.
    If the rig's patch stops intercepting the production resolver, the
    empty case reads the decoy and fails even on a fresh public checkout.
    """

    # (1) a seeded directory IS what the constructor reads.
    seeded = tmp_path / "seeded"
    seeded.mkdir()
    (seeded / "soft_allow_pending_counters.toml").write_text(
        "[[entries]]\n"
        'process_name = "journeytest.exe"\n'
        'class_name = "JourneyClass"\n'
        'control_type = "ButtonControl"\n'
        "count = 2\n"
        'last_updated_at = "2026-08-29T00:00:00+00:00"\n',
        encoding="utf-8",
    )
    (seeded / "soft_allow_declined_tuples.toml").write_text(
        "[[entries]]\n"
        'process_name = "journeytest.exe"\n'
        'class_name = "JourneyClass"\n'
        'control_type = "ButtonControl"\n'
        'added_at = "2026-08-29T00:00:00+00:00"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(utils_system, "get_user_data_dir", lambda: seeded)
    async with _real_logic_controller(seeded) as rig:
        controller = rig.controller
        assert controller._resolve_declined_path().parent == seeded
        assert controller._resolve_pending_counters_path().parent == seeded
        assert controller._grant_prompt_no_suppressed == {
            ("journeytest.exe", "JourneyClass", "ButtonControl"),
        }
        assert controller.click_counter.get_count(
            "journeytest.exe", "JourneyClass", "ButtonControl",
        ) == 2

    # (2) an empty directory -- what every journey gets -- loads nothing.
    empty = tmp_path / "empty"
    empty.mkdir()
    async with _real_logic_controller(empty) as rig:
        controller = rig.controller
        assert controller._grant_prompt_no_suppressed == set()
        assert controller.click_counter.snapshot_entries() == []
        assert controller._resolve_declined_path().parent == empty

    # The rig's patch is undone on exit, restoring the nonempty decoy.
    assert utils_system.get_user_data_dir() == seeded
    assert seeded != empty


def _abs_casefold(path: Path) -> str:
    """Absolute, casefolded path text, for comparing paths on Windows.

    Windows paths are case-insensitive, and a relative ``"config.toml"``
    would slip past a literal string comparison, so both sides of every path
    assertion below go through this.
    """

    return str(Path(path).resolve()).casefold()


# The rig's configuration isolation is proved by the next THREE tests, one
# per part, rather than by one test with three sections. They are separate so
# the mutation gate's output names which parts a mutation turns red: a single
# test stops at its first failing assertion and reports only that one.
#
# services/wheelhouse/config.toml IS tracked -- measured 2026-08-29 with
# ``git ls-files --error-unmatch`` and ``git check-ignore -v`` -- but it is
# also the file a user edits, so its working-tree content is the machine
# owner's state rather than the repository's. ``[click]
# overlay_settle_after_click`` shipping off today is not an invariant: the day
# the owner turns it on deliberately, a journey that read that file would
# change behaviour for a legitimate local setting.
#
# 7 is held by neither the shipped example (config.toml.example:255) nor the
# code default (ui/click_config.py:681), so a seeded 7 cannot be read out of
# any other file.
_SEEDED_NOTICE_MAX = 7
_MACHINE_CONFIG_PATH = _SERVICE_DIR / "config.toml"


def _write_seeded_config(tmp_path: Path) -> Path:
    """A configuration file carrying a value no default and no shipped file holds."""

    seeded = tmp_path / "seeded-config.toml"
    seeded.write_text(
        f"[click]\nenabled = true\nnotice_max_names = {_SEEDED_NOTICE_MAX}\n",
        encoding="utf-8",
    )
    return seeded


async def test_config_part_1_a_seeded_config_is_what_the_rig_reads(tmp_path):
    """Part 1: the ``config_path`` argument is load-bearing, not decorative."""

    data_dir = tmp_path / "seeded-data"
    data_dir.mkdir()
    async with _real_logic_controller(
        data_dir, config_path=_write_seeded_config(tmp_path),
    ) as rig:
        assert (
            rig.controller.click_config.notice_max_names == _SEEDED_NOTICE_MAX
        )


async def test_config_part_2_the_journey_path_is_the_example_not_the_machine_file(
    tmp_path,
):
    """Part 2: the file every journey gets is the tracked example."""

    data_dir = tmp_path / "journey-data"
    data_dir.mkdir()
    async with _real_logic_controller(data_dir) as rig:
        used = Path(rig.controller.config_service.config_path)
        assert _abs_casefold(used) == _abs_casefold(_JOURNEY_CONFIG_PATH)
        assert _abs_casefold(used) != _abs_casefold(_MACHINE_CONFIG_PATH)
        # And the example really does differ from the seeded value, or part 1
        # would have proved nothing.
        assert (
            rig.controller.click_config.notice_max_names != _SEEDED_NOTICE_MAX
        )


async def test_config_part_3_constructing_the_rig_never_opens_the_machine_config(
    tmp_path,
):
    """Part 3: every file opened during construction, recorded.

    This is the only one of the three that catches a FUTURE code path reading
    config.toml DIRECTLY, around ``ConfigService`` entirely, which is the
    failure the hold actually guards. ``features/window_mover.py:49`` shows
    the shape: ``CONFIG = ConfigService()`` at module level, so merely
    importing that module reads the machine file. It is not imported on this
    path today (measured: absent from ``sys.modules`` after construction),
    and this test is what would notice if that changed.

    The recorder wraps construction ONLY, delegates every call to the real
    ``open``, and ``patch`` restores ``builtins.open`` on exit of its own
    ``with`` block whatever happens inside it.
    """

    import builtins
    import os

    opened: list[str] = []
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            opened.append(os.fspath(file))
        return real_open(file, *args, **kwargs)

    data_dir = tmp_path / "recorded-data"
    data_dir.mkdir()
    ctx = _real_logic_controller(data_dir)
    with patch("builtins.open", recording_open):
        await ctx.__aenter__()
    try:
        normalised = {_abs_casefold(Path(p)) for p in opened}
        assert _abs_casefold(_JOURNEY_CONFIG_PATH) in normalised, (
            "the recorder never saw the example configuration opened, so its "
            "silence about config.toml would prove nothing; the recorder is "
            "not wrapping the read it is supposed to be watching"
        )
        assert _abs_casefold(_MACHINE_CONFIG_PATH) not in normalised, (
            f"constructing the journey rig opened {_MACHINE_CONFIG_PATH}; "
            "every journey would then depend on state the repository does "
            f"not control. Paths opened: {sorted(normalised)}"
        )
    finally:
        await ctx.__aexit__(None, None, None)


async def test_journey_rig_starts_with_no_declined_or_pending_state(journey_rig):
    """The fixture every journey uses is the isolated one, not the real dir."""

    controller = journey_rig.controller
    assert controller._resolve_declined_path().parent == journey_rig.data_dir
    assert controller._resolve_pending_counters_path().parent == journey_rig.data_dir
    assert controller._grant_prompt_no_suppressed == set()
    assert controller.click_counter.snapshot_entries() == []


# ---------------------------------------------------------------------------
# Journey 1 -- two numbers spoken close together
# ---------------------------------------------------------------------------


async def test_journey_1_second_number_before_the_first_reply_never_reaches_input(
    journey_rig,
):
    """Journey 1: the second click must not run against the old list.

    Badges 16 and 17 are on screen. The user says "click sixteen"; the
    command reaches Input. Before Input answers, the user says "click
    seventeen". Input runs commands strictly in order, so a second command
    admitted now would execute AFTER the first press has already changed the
    screen -- badge 17 would name a different control by then. The guard at
    main.py:3544-3565 (wh-overlay-slow-uia-stale-badges.8, merged as commit
    7e7bca0e) refuses it loudly instead.

    Everything the assertions read is a real production artifact: the frame
    is the one the real sender chain wrote into real shared memory, de-framed
    through production's ``_read_envelope``; the notice is on the real
    ``multiprocessing.Queue`` the real ``StateManager`` owns.
    """

    controller = journey_rig.controller
    command_ready_event = journey_rig.command_ready_event
    session_id, generation = _paint_badges(
        controller, "snap-journey-1", 16, 17,
    )
    # Anything the construction of the rig itself may have queued is not part
    # of this journey.
    await _drain_gui_queue(journey_rig.state_to_gui_queue, settle_s=0.1)

    # --- the user says "click sixteen" -------------------------------------
    await controller.forward_click_element(
        _speak_click("sixteen"), "trace-sixteen",
    )
    await _await_frame(command_ready_event)

    action, params, has_params, request_id, trace_id = _read_command_frame(
        journey_rig.shm,
    )

    # ASSERTION 1: exactly one command reached the Input side, and it names
    # the item badge 16 was painted for.
    assert action == "click_snapshot_item"
    assert has_params
    assert params["snapshot_id"] == "snap-journey-1"
    assert params["item_id"] == "snap-journey-1-item-16"
    assert params["trace_id"] == "trace-sixteen"
    assert trace_id == "trace-sixteen"
    assert request_id is not None
    # overlay_resolved_pair rides along so Input can refuse a click from a
    # generation an earlier click already consumed (main.py:3690-3697).
    assert params["overlay_session_id"] == session_id
    assert params["paint_generation"] == generation

    # Copy-then-clear, exactly as the Input loop does (input_proc.py:1066).
    # This frees the frame, so a second command WOULD be delivered if Logic
    # sent one -- which is what makes the next assertion about Logic and not
    # about transport backpressure.
    command_ready_event.clear()

    # The first click is still awaiting its reply, and nothing ever answers.
    assert controller._overlay_click_in_flight == (
        "snap-journey-1", "snap-journey-1-item-16", "trace-sixteen",
    )

    # --- the user says "click seventeen" before any reply -------------------
    await controller.forward_click_element(
        _speak_click("seventeen"), "trace-seventeen",
    )

    # ASSERTION 2: no second command was sent.
    await _assert_no_further_frame(
        journey_rig,
        window_s=0.3,
        why=(
            "a second click_snapshot_item frame reached Input while the "
            "first click was still in flight"
        ),
    )

    # ASSERTION 3: the second utterance produced a refusal notice on the real
    # GUI state queue, carrying the reason the guard names.
    actions = await _drain_gui_queue(journey_rig.state_to_gui_queue)
    notices = [a for a in actions if a.get("action") == "show_click_notice"]
    assert len(notices) == 1, (
        f"expected exactly one click notice, got {actions!r}"
    )
    notice = notices[0]
    assert notice["outcome"] == "execution_failed"
    assert notice["reason"] == "overlay_click_in_flight"
    assert notice["trace_id"] == "trace-seventeen"
    assert notice["snapshot_id"] == "snap-journey-1"

    # And the first click is still the one holding the guard -- the refusal
    # did not disturb it.
    assert controller._overlay_click_in_flight == (
        "snap-journey-1", "snap-journey-1-item-16", "trace-sixteen",
    )


# ---------------------------------------------------------------------------
# Journey 3 -- focus moves while the click settles
# ---------------------------------------------------------------------------
#
# THIS JOURNEY'S SHAPE DIFFERS FROM ITS ONE-LINE DESCRIPTION, and the code is
# what it follows. The description says "an unrelated focus move cancels the
# repaint". The state machine deliberately does NOT cancel: the FOCUS_CHANGE
# arm of ``_on_post_click_settling`` returns NO_OP and says why in its own
# comment (click_overlay_state.py:1301-1307) -- "a click that opens a dialog
# CHANGES the foreground window, and that is the common case for 'click N'.
# Cancelling here would make it behave like Option A, which David rejected."
# Refusing a repaint against the wrong window is done by the INTEGRATION, at
# the commit fence in ``_overlay_dispatch_build`` (main.py:6355-6396), through
# a full window-identity check. So this journey has two halves, one per
# component, and a test written to the description's literal sentence would
# have pinned the design that was rejected.
#
# Half A is below. Half B -- a settle reply naming a mismatched window is
# refused at the commit fence -- needs a crafted Input reply to cross the real
# response queue, and is the following test.


def _write_settling_config(tmp_path: Path) -> Path:
    """A configuration whose ``[click]`` block turns the post-click settle on.

    ``overlay_settle_after_click`` ships OFF (ui/click_config.py:223), and
    that stays true: this writes a file into a pytest ``tmp_path`` that
    nothing outside this test reads. The exclusion on the flag covers the
    shipped runtime default -- ``services/wheelhouse/config.toml`` and
    anything a user would experience -- not a value handed to a constructor
    inside a test. Reaching the settle path any other way is impossible:
    main.py:4270 shows the whole path is inert with the flag off.

    The rig is built from this file rather than by reaching in and setting
    the attribute, so the machine is the one the REAL factory
    (``_build_overlay_state_machine``, main.py:458) produced from a REAL
    ``ClickConfig`` -- the config-to-machine wiring is exercised, not assumed.
    """

    settling = tmp_path / "settling-config.toml"
    settling.write_text(
        "[click]\nenabled = true\noverlay_settle_after_click = true\n",
        encoding="utf-8",
    )
    return settling


async def test_journey_3a_a_dialog_opening_does_not_cancel_the_settling_read(
    tmp_path,
):
    """Journey 3, half A: the machine stays, and the settle read goes out.

    The user says "click 3", the click succeeds, and it opens a dialog --
    which is the COMMON case for "click N", not an edge case. The foreground
    window therefore changes while the machine is settling.

    What must happen: nothing cancels. The machine stays in
    POST_CLICK_SETTLING with its pin intact and its deadline running, and the
    settle build that was dispatched on entry is still the one outstanding. A
    machine that cancelled here would clear the numbers and never bring them
    back, which is the behaviour that was considered and rejected.
    """

    from services.wheelhouse.click_overlay_state import (
        BuildReason,
        EffectKind,
        OverlayOutcome,
    )

    data_dir = tmp_path / "journey-3a-data"
    data_dir.mkdir()
    async with _real_logic_controller(
        data_dir, config_path=_write_settling_config(tmp_path),
    ) as rig:
        controller = rig.controller
        machine = controller.click_overlay_state
        assert machine.settle_after_click, (
            "the real factory did not carry [click] overlay_settle_after_click "
            "onto the machine; with the flag off main.py:4270 makes the whole "
            "settle path inert and this journey would pin nothing"
        )

        session_id, generation = _paint_badges(
            controller, "snap-journey-3", 3, 4,
        )

        # --- the click succeeds, and the machine begins to settle -----------
        completed = machine.apply(
            OverlayEvent(OverlayEventKind.CLICK_COMPLETE)
        )
        assert completed.outcome is OverlayOutcome.ACCEPTED
        assert machine.state is OverlayState.POST_CLICK_SETTLING
        # Entry clears the badges at once, bumps the generation, arms this
        # state's own deadline, and dispatches the settle read against the
        # pin that was on screen when the user clicked
        # (click_overlay_state.py:832-866).
        kinds = [e.kind for e in completed.effects]
        assert EffectKind.DISPATCH_CLEAR in kinds
        assert EffectKind.ARM_TIMER in kinds
        assert EffectKind.DISPATCH_BUILD in kinds
        assert kinds.index(EffectKind.ARM_TIMER) < kinds.index(
            EffectKind.DISPATCH_BUILD
        ), (
            "ARM_TIMER must precede DISPATCH_BUILD, or settle_deadline_ms "
            "starts only after the Input round trip and bounds nothing "
            "(click_overlay_state.py:835-848)"
        )
        settle_build = next(
            e for e in completed.effects if e.kind is EffectKind.DISPATCH_BUILD
        )
        assert settle_build.build_reason is BuildReason.SETTLE
        assert settle_build.snapshot_id == "snap-journey-3", (
            "the settle read is compared against the pin that was on screen "
            "when the user clicked, not against a fresh one"
        )
        assert machine.pinned_snapshot_id == "snap-journey-3"
        assert machine.paint_generation == generation + 1
        assert machine.overlay_session_id == session_id

        # --- the dialog the click opened takes the foreground ---------------
        moved = machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE))

        # THE ASSERTION THIS JOURNEY EXISTS FOR. Nothing is cancelled.
        assert moved.outcome is OverlayOutcome.NO_OP, (
            "the state machine cancelled the settling read on a focus change; "
            "that is Option A, which was considered and rejected -- a click "
            "that opens a dialog is the common case, and cancelling on it "
            "clears the numbers and never brings them back"
        )
        assert moved.effects == ()
        assert machine.state is OverlayState.POST_CLICK_SETTLING
        assert machine.pinned_snapshot_id == "snap-journey-3"
        assert machine.overlay_session_id == session_id
        assert machine.paint_generation == generation + 1

        # A spoken number is refused for the whole of this state rather than
        # resolving against a list that is no longer on the screen.
        held = machine.apply(OverlayEvent(OverlayEventKind.CLICK_N))
        assert held.outcome is OverlayOutcome.NO_OP
        assert machine.state is OverlayState.POST_CLICK_SETTLING


# ---------------------------------------------------------------------------
# The display-process rig (journeys 4 and 6)
# ---------------------------------------------------------------------------
#
# WHAT IS REAL HERE. A real ``GuiManager``, built through its real
# ``__init__``, which constructs a real ``OverlayPaintWindowManager`` on its
# own (gui.py:1157-1167). The paint and clear lifecycle is the real one, the
# badge surface is drawn by the real Qt rendering path
# (``_render_monitor_surface`` -> ``QImage`` -> the real GDI DIB built by
# ``shared/overlay_bitmap.py:build_layered_dib``), the DPI conversion is the
# real ``resolve_overlay_paint_rect``, and the lease bookkeeping is the real
# ``_arm_overlay_lease`` / ``_cancel_overlay_lease`` /
# ``_on_overlay_lease_expired`` / ``_arm_teardown_retry_if_pending``.
#
# WHAT IS FAKED, and nothing else: the Win32 calls that put a window on the
# PHYSICAL screen, and the enumeration of the physical screens themselves.
# No journey may create a real per-monitor layered window --
# wh-pytest-flaky-segfault is open and incidental native windows are exactly
# the surface it covers.


# A monitor handle no real device holds. ``_enumerate_native_monitors``
# returns real HMONITOR values from the OS, so a badge window recorded
# against THIS number proves the paint read the injected topology.
_FAKE_HMONITOR = 0x7011


class _FakeQTimer:
    """A single-shot QTimer stand-in whose firing is the journey's decision.

    The two intervals these journeys turn on are 90 s
    (gui._OVERLAY_LEASE_DEFAULT_MS) and 2 s (gui._OVERLAY_TEARDOWN_RETRY_MS),
    against the 30 s per-test cap (pyproject.toml ``timeout``). Neither is
    available in real time, and no Qt event loop runs inside an asyncio test
    to deliver them anyway. So the timer RECORDS the interval production
    asked for -- which is itself an assertion target -- and fires only when a
    journey says so. Everything the timer drives is the real GuiManager
    method bound to the real instance.
    """

    def __init__(self, slot) -> None:
        self._slot = slot
        self.interval_ms = None
        self.running = False
        self.starts = 0

    def start(self, interval_ms) -> None:
        self.interval_ms = int(interval_ms)
        self.running = True
        self.starts += 1

    def stop(self) -> None:
        self.running = False

    def fire(self) -> None:
        assert self.running, (
            "the journey fired a timer production never armed; the interval "
            "was never requested, so nothing would have happened on a real "
            "Qt loop either"
        )
        self.running = False  # single-shot, exactly as GuiManager arms it
        self._slot()


@dataclass
class DisplayRig:
    """A real display process for the badge windows.

    ``manager`` is the real ``OverlayPaintWindowManager`` the real
    ``GuiManager.__init__`` built; ``manager._windows`` is the manager's own
    record of which badge windows exist, keyed by hmonitor. No assertion in
    these journeys reads a pixel -- the surface is rendered for real so the
    render path cannot silently break, but what is asserted is the
    ``overlay_state_changed`` dict and that window record.
    """

    gui: Any
    manager: Any
    module: Any
    lease_timer: _FakeQTimer
    teardown_timer: _FakeQTimer
    commands_to_logic_queue: Any
    user32: Any


def _fake_overlay_win32() -> tuple:
    """Stand-ins for the Win32 entry points that reach the physical screen.

    ``CreateWindowExW`` (overlay_paint_window.py:852) makes the on-screen
    layered window; ``ShowWindow`` presents it; ``GetDC(None)`` /
    ``ReleaseDC`` acquire and release the screen DC the composite blits
    against (:884-894); ``DestroyWindow`` (:906) takes the window off.
    ``RegisterClassExW`` and ``GetModuleHandleW`` exist only to make
    ``CreateWindowExW`` possible, and ``GetLastError`` only reports on it.
    The manager stores ``_gdi32`` (:1009) and never calls it -- grep for
    ``self._gdi32`` in overlay_paint_window.py returns that one assignment --
    so the gdi32 stand-in is there for the attribute and nothing more.
    """

    from unittest.mock import MagicMock

    user32 = MagicMock(name="user32")
    kernel32 = MagicMock(name="kernel32")
    gdi32 = MagicMock(name="gdi32")
    user32.RegisterClassExW.return_value = 1
    user32.CreateWindowExW.return_value = wintypes.HWND(0xC0DE)
    user32.ShowWindow.return_value = True
    user32.DestroyWindow.return_value = True
    user32.GetDC.return_value = wintypes.HDC(0x1111)
    user32.ReleaseDC.return_value = 1
    user32.DefWindowProcW.return_value = 0
    kernel32.GetModuleHandleW.return_value = wintypes.HMODULE(1)
    kernel32.GetLastError.return_value = 0
    return user32, kernel32, gdi32


def _fake_monitor(hmonitor: int = _FAKE_HMONITOR):
    """One synthetic monitor for the injected display topology."""

    from PySide6.QtCore import QRect

    from shared.monitor_geometry import _NativeMonitor

    return _NativeMonitor(
        hmonitor=hmonitor, rect_phys=QRect(0, 0, 1920, 1080), dpi=96,
    )


@contextmanager
def _overlay_win32_faked(monitors):
    """Fake the screen-touching seams; leave every other seam real.

    ``overlay_paint_window.ctypes`` is the seam the module's own docstring
    names ("which the test harness patches via ``overlay_paint_window
    .ctypes``", :966-974), and the manager reads
    ``ctypes.windll.user32/kernel32/gdi32`` ONCE in ``__init__`` (:1007-1009)
    and hands the same objects to every ``_OverlayWindow`` it builds
    (:1950-1956), so one fake reaches every window. The real ``ctypes``
    attributes the WNDCLASS registration and the 64-bit-safe prototypes need
    are passed straight through: a mock ``WINFUNCTYPE`` or ``Structure``
    would break code that has nothing to do with the screen.

    ``_screens`` (:736) and ``_enumerate_native_monitors`` are the module's
    two declared topology seams -- ``_screens`` carries a docstring saying it
    is patched in tests -- and they are what makes a paint independent of the
    monitors physically attached to the machine running the suite.

    ``composite_layered_window`` is the ``UpdateLayeredWindow`` call
    (:884-894). It is faked at the ``overlay_paint_window`` name rather than
    inside ``shared/overlay_bitmap.py`` so ``build_layered_dib`` -- which
    touches only off-screen GDI memory -- stays REAL and the whole
    Qt-to-DIB bridge is exercised.
    """

    import ctypes as real_ctypes

    user32, kernel32, gdi32 = _fake_overlay_win32()
    with patch("overlay_paint_window.ctypes") as mock_ctypes:
        mock_ctypes.windll.user32 = user32
        mock_ctypes.windll.kernel32 = kernel32
        mock_ctypes.windll.gdi32 = gdi32
        for name in (
            "POINTER", "byref", "sizeof", "WINFUNCTYPE", "WinError",
            "c_ssize_t", "c_int", "Structure",
        ):
            setattr(mock_ctypes, name, getattr(real_ctypes, name))

        import overlay_paint_window as overlay_module

        with patch.object(
            overlay_module,
            "_enumerate_native_monitors",
            return_value=list(monitors),
        ), patch.object(
            overlay_module, "_screens", return_value=[],
        ), patch.object(
            overlay_module, "composite_layered_window", return_value=True,
        ):
            yield overlay_module, user32, kernel32


@contextmanager
def _real_display_process(
    state_from_logic_queue,
    commands_to_logic_queue,
    shutdown_event,
    *,
    monitors,
    config=None,
):
    """Build a real ``GuiManager`` over the journey's real queues.

    HERMETICITY. Two machine-specific sources reach this process in
    production and neither may reach a journey:

      * ``services/wheelhouse/config.toml`` -- ``gui_process_target`` reads
        the whole file with ``ConfigService().get_config()`` and hands it to
        ``GuiManager(config=...)``, which is where the overlay badge
        appearance comes from (gui.py:1155-1167). ``config`` defaults to
        ``None`` here, so the manager is built from the shipped
        ``ClickConfig`` defaults and nothing on this disk.
      * the monitors physically attached to this machine, and the Qt screen
        list -- injected through the two seams ``_overlay_win32_faked``
        patches.

    Both are PROVEN, not asserted, by
    ``test_display_rig_reads_the_injected_config_and_topology``.

    ``gui_shm_name`` is left None so no shared-memory block is attached: the
    activity block is the floating button's business, not the overlay's.
    """

    from unittest.mock import MagicMock

    with _overlay_win32_faked(monitors) as (overlay_module, user32, _kernel32):
        with patch("gui.FloatingButton"), patch("gui.WorkingDialog"), \
                patch("gui.pystray") as mock_pystray, patch("gui.QTimer"):
            mock_pystray.Icon.return_value = MagicMock()
            from gui import GuiManager

            gui_manager = GuiManager(
                shutdown_event,
                commands_to_logic_queue,
                state_from_logic_queue,
                config=config,
            )
        assert gui_manager._overlay_manager is not None, (
            "GuiManager could not build an OverlayPaintWindowManager; its "
            "constructor swallows a ctypes failure (gui.py:1168) and leaves "
            "the overlay unavailable, so a journey would assert nothing"
        )
        assert gui_manager._overlay_manager._user32 is user32, (
            "the ctypes patch did not reach the module gui.py imports; two "
            "distinct module objects for overlay_paint_window would put the "
            "real Win32 calls back in play"
        )
        # Every QTimer(self) in __init__ returned the SAME patched mock, and
        # a mock never fires. Give the two timers these journeys depend on
        # their own controllable stand-in, wired to the REAL slots
        # GuiManager.__init__ connects (gui.py:1182 and :1201).
        lease_timer = _FakeQTimer(gui_manager._on_overlay_lease_expired)
        teardown_timer = _FakeQTimer(gui_manager._on_overlay_teardown_retry)
        gui_manager._overlay_lease_timer = lease_timer
        gui_manager._overlay_teardown_retry_timer = teardown_timer
        try:
            yield DisplayRig(
                gui=gui_manager,
                manager=gui_manager._overlay_manager,
                module=overlay_module,
                lease_timer=lease_timer,
                teardown_timer=teardown_timer,
                commands_to_logic_queue=commands_to_logic_queue,
                user32=user32,
            )
        finally:
            # Destroys every window the journey left behind. The Win32 call
            # is the fake one, so this only clears the manager's records.
            gui_manager._overlay_manager.clear_all()


@pytest.fixture
def display_rig(journey_rig, qapp, mock_editor_window):
    """A real display process wired to ``journey_rig``'s real queues.

    ``qapp`` and ``mock_editor_window`` are requested here rather than as a
    module-level ``usefixtures`` mark so journey 1 and the two isolation
    tests keep the exact environment they were written and verified in.
    ``mock_editor_window`` matters: ``GuiManager.__init__`` builds a real
    ``TerminalDictationEditorWindow`` QDialog otherwise, and an incidental
    native dialog is wh-pytest-flaky-segfault surface.
    """

    commands_to_logic_queue = MpQueue()
    try:
        with _real_display_process(
            journey_rig.state_to_gui_queue,
            commands_to_logic_queue,
            journey_rig.controller.shutdown_event,
            monitors=[_fake_monitor()],
        ) as rig:
            yield rig
    finally:
        commands_to_logic_queue.close()


# ---------------------------------------------------------------------------
# Driving the Logic to display boundary
# ---------------------------------------------------------------------------


def _paint_badges_awaiting_display(
    controller: LogicController, snapshot_id: str, *display_numbers: int,
):
    """Drive the machine to PAINT_IN_FLIGHT and hand back its paint effect.

    The sibling of ``_paint_badges``, and deliberately not a caller of it:
    journey 1 needs a machine already in PAINTED, and these journeys need
    the PAINT_ACK to arrive from the display process instead, so the two
    stop at different states. Everything else is the same pure setup --
    real transitions on the controller's OWN machine and cache.

    Returns ``(overlay_session_id, paint_generation, dispatch_paint_effect)``.
    The effect is the one the MACHINE emitted, not one built here, which is
    what makes the dispatch fence at main.py:6702 (the pair plus the pinned
    snapshot) a real gate rather than a formality.
    """

    from services.wheelhouse.click_overlay_state import EffectKind

    machine = controller.click_overlay_state
    # "show numbers" spoken over painted badges is a REFRESH: the machine
    # bumps the paint generation and keeps the old badges up until the new
    # paint is acked. Both entries land in a state whose BUILD_RESPONSE
    # emits the DISPATCH_PAINT this helper hands back.
    was_painted = machine.state is OverlayState.PAINTED
    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    session_id, generation = machine.overlay_session_id, machine.paint_generation
    result = machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=session_id,
            paint_generation=generation,
            snapshot_id=snapshot_id,
        )
    )
    assert machine.state is (
        OverlayState.REFRESH_IN_FLIGHT if was_painted
        else OverlayState.PAINT_IN_FLIGHT
    )
    controller.click_snapshot_summary_cache.put(
        snapshot_id, _summary(snapshot_id, *display_numbers),
    )
    paints = [
        e for e in result.effects if e.kind is EffectKind.DISPATCH_PAINT
    ]
    assert len(paints) == 1, (
        f"expected exactly one DISPATCH_PAINT effect, got {result.effects!r}"
    )
    return session_id, generation, paints[0]


async def _await_true(predicate, *, timeout_s: float = 3.0, why: str) -> None:
    """Poll ``predicate`` until it holds, or fail. Never sleeps a fixed span."""

    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{why} (waited {timeout_s}s)")
        await asyncio.sleep(0.005)


async def _pump_display(display, *, until, timeout_s: float = 3.0, why: str):
    """Run the display's REAL state-queue dispatch until ``until`` holds.

    ``GuiManager._check_queues_and_events`` (gui.py:1607) is the production
    consumer: it drains ``state_from_logic_queue`` and routes every action,
    ``paint_overlay`` / ``clear_overlay`` / ``reset_overlay`` included. It is
    normally driven by a QTimer, so a journey calls it directly. Called in a
    loop because a real ``multiprocessing.Queue`` hands items to a feeder
    thread, so an item is not readable the instant ``put_nowait`` returns.
    """

    deadline = time.monotonic() + timeout_s
    while True:
        display.gui._check_queues_and_events()
        if until():
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{why} (waited {timeout_s}s)")
        await asyncio.sleep(0.005)


def _feed_gui_commands_to_logic(controller: LogicController, commands) -> None:
    """Route dicts the display emitted through production's own dispatch.

    ``_build_gui_handler_map`` IS the production routing table, and the two
    statements below are the two production applies to each command
    (main.py:10963-10970). They are written out here for the same reason
    ``_read_command_frame`` writes out its three: the only production caller
    is ``_listen_for_gui_commands``, a
    ``while not self.shutdown_event.is_set()`` loop around a blocking
    ``asyncio.to_thread(queue.get, ...)`` call, which cannot be asked for
    exactly one message and cannot hand a journey the dict BEFORE Logic
    consumes it -- and both journeys here assert on the dict the display
    produced.

    Every overlay entry in the map is an async handler already wrapped in a
    ``create_task_with_error_handling`` lambda by the map itself, so calling
    it schedules the same background task production schedules.
    """

    for command in commands:
        handler = controller._build_gui_handler_map(command).get(
            command.get("action")
        )
        assert handler is not None, (
            f"production has no GUI handler for {command.get('action')!r}"
        )
        handler()


def _badge_window_monitors(manager) -> list:
    """The manager's own record of which badge windows exist, by hmonitor.

    ``_windows`` is the dict ``_do_paint`` creates windows into and every
    teardown path empties (overlay_paint_window.py:1918-1957, :3251-3277).
    There is no public accessor -- ``teardown_pending`` and
    ``has_deferred_teardown_ack`` are the only public state -- so this reads
    the attribute, as the module's own unit tests do.
    """

    return sorted(manager._windows)


# ---------------------------------------------------------------------------
# The display rig's isolation is real, the same three ways
# ---------------------------------------------------------------------------


async def test_display_rig_reads_the_injected_config_and_topology(
    qapp, mock_editor_window,
):
    """The display rig loads test inputs and never this machine's state.

    ``journey_rig`` isolates the Logic process's user-data directory. The
    display process adds TWO machine-specific sources of its own, and each
    is proved the same three ways the user-data isolation is proved: the
    un-isolated source really holds data (read with the production reader),
    a seeded source is what the code reads, and the source every journey
    actually gets loads nothing.

    SOURCE 1 -- the service configuration file. The shipped display process
    reads the whole of it with ``ConfigService().get_config()`` and hands it
    to ``GuiManager(config=...)``, which derives the overlay badge
    appearance from it (gui.py:1155-1167).

    SOURCE 2 -- the monitors physically attached to this machine, read by
    ``overlay_paint_window._enumerate_native_monitors``. Without the seam a
    journey's badge geometry, and whether any badge window is created at
    all, would depend on the display hardware of whoever runs the suite.
    """

    from services.wheelhouse.config_service import ConfigService
    from services.wheelhouse.ui.click_config import DISABLED_CLICK_CONFIG

    shipped_font_pt = DISABLED_CLICK_CONFIG.overlay_badge_font_pt
    seeded_font_pt = 31
    assert seeded_font_pt != shipped_font_pt, (
        "the seeded value must differ from the shipped default, or the "
        "seeded step below could pass without reading the seeded config"
    )

    # --- SOURCE 1, third point first: a real service configuration file
    # parses to a non-empty mapping, read with the production reader the
    # display process itself uses. The file read here is the TRACKED
    # config.toml.example, not services/wheelhouse/config.toml: no test path
    # in this file may read the latter, because its working-tree content is
    # the machine owner's, not the repository's.
    real_config = ConfigService(str(_JOURNEY_CONFIG_PATH)).get_config()
    assert real_config, (
        f"this check needs {_JOURNEY_CONFIG_PATH.name} to parse to a "
        "non-empty mapping to be meaningful; the production reader "
        "ConfigService(...).get_config() returned nothing"
    )

    monitor = _fake_monitor()

    # --- SOURCE 1, first point: a SEEDED config is what GuiManager reads.
    with _real_display_process(
        MpQueue(), MpQueue(), MpEvent(),
        monitors=[monitor],
        config={"click": {"overlay_badge_font_pt": seeded_font_pt}},
    ) as seeded:
        assert seeded.manager._badge_font_pt == seeded_font_pt

    # --- SOURCE 1, second point: the config every journey gets is the
    # shipped default, carried in from nothing on this disk.
    with _real_display_process(
        MpQueue(), MpQueue(), MpEvent(), monitors=[monitor], config=None,
    ) as empty:
        assert empty.manager._badge_font_pt == shipped_font_pt
        assert empty.manager._badge_font_pt != seeded_font_pt

    # --- SOURCE 2, third point: this machine really has monitors, and none
    # of them is the synthetic one. Read with the production enumerator,
    # unpatched -- so the seeded step below cannot be passing by accident.
    from shared.monitor_geometry import _enumerate_native_monitors

    real_monitors = _enumerate_native_monitors()
    assert real_monitors, (
        "this check needs at least one enumerated monitor to be meaningful; "
        "_enumerate_native_monitors() returned nothing on this machine"
    )
    assert _FAKE_HMONITOR not in {m.hmonitor for m in real_monitors}, (
        f"the synthetic hmonitor {_FAKE_HMONITOR:#x} collides with a real "
        "one on this machine; pick another"
    )

    # --- SOURCE 2, first point: the SEEDED topology is what the paint reads.
    # The badge window lands on the synthetic monitor, which no real device
    # on this machine holds.
    with _real_display_process(
        MpQueue(), MpQueue(), MpEvent(), monitors=[monitor],
    ) as seeded_topology:
        result = seeded_topology.manager.paint(
            _summary("snap-isolation", 1, 2),
            overlay_session_id=1,
            paint_generation=0,
        )
        assert result["state"] == "painted"
        assert result["monitor_ids"] == [_FAKE_HMONITOR]
        assert _badge_window_monitors(seeded_topology.manager) == [
            _FAKE_HMONITOR
        ]

    # --- SOURCE 2, second point: an EMPTY topology creates no badge window
    # at all, so the seam is load-bearing rather than decorative.
    with _real_display_process(
        MpQueue(), MpQueue(), MpEvent(), monitors=[],
    ) as no_topology:
        result = no_topology.manager.paint(
            _summary("snap-isolation", 1, 2),
            overlay_session_id=1,
            paint_generation=0,
        )
        assert result["state"] == "painted"
        assert result["monitor_ids"] == []
        assert _badge_window_monitors(no_topology.manager) == []


async def _drain_expecting(
    queue, *, at_least: int = 1, timeout_s: float = 3.0, why: str,
) -> list:
    """Drain a real IPC queue, waiting only until the expected items land.

    ``_drain_gui_queue`` burns its whole settle window on every call, which
    is right when the assertion is that NOTHING arrived. These journeys
    mostly assert that something specific DID arrive, so this waits for the
    first expected items and then takes one short sweep for anything queued
    behind them -- so an unexpected extra message is still caught.
    """

    drained: list = []
    deadline = time.monotonic() + timeout_s
    while len(drained) < at_least:
        try:
            drained.append(queue.get_nowait())
        except queue_mod.Empty:
            if time.monotonic() > deadline:
                raise AssertionError(f"{why}; drained {drained!r}")
            await asyncio.sleep(0.005)
    drained.extend(await _drain_gui_queue(queue, settle_s=0.05))
    return drained


async def _badges_onto_the_display(
    rig: JourneyRig, display: DisplayRig, snapshot_id: str, *display_numbers: int,
) -> tuple[int, int]:
    """Carry one overlay paint from Logic to the display and back.

    The whole round trip is production code over the journey's real
    transport: the machine's own DISPATCH_PAINT effect goes through
    ``_overlay_dispatch_paint`` (main.py:6611) onto the real
    ``multiprocessing.Queue``; the display's real
    ``_check_queues_and_events`` takes it off and routes it to
    ``_handle_paint_overlay``, which drives the real paint lifecycle and
    arms the real badge lease; the ``overlay_state_changed`` dict it returns
    goes back through production's own GUI-command dispatch, and the
    machine reaches PAINTED because the DISPLAY said so.

    Returns the ``(overlay_session_id, paint_generation)`` pair now on
    screen.
    """

    controller = rig.controller
    session_id, generation, paint_effect = _paint_badges_awaiting_display(
        controller, snapshot_id, *display_numbers,
    )
    await controller._overlay_dispatch_paint(paint_effect, "trace-paint")
    # The lease pair, not "a window exists": on a refresh a badge window is
    # already up from the previous generation, so a window-existence
    # predicate would be true before the pump and return without delivering
    # anything. The lease pair carries the generation, so it can only become
    # true once THIS paint has been applied.
    await _pump_display(
        display,
        until=lambda: display.gui._overlay_lease_pair == (
            session_id, generation,
        ),
        why="the display never applied the paint Logic sent",
    )
    assert _badge_window_monitors(display.manager) == [_FAKE_HMONITOR]
    # The lease is armed, at the interval the shipped GUI asks for. 90 s is
    # not available inside a 30 s per-test cap, so the interval is asserted
    # and the firing is the journey's to decide (see _FakeQTimer).
    #
    # The literal, deliberately, and not gui_module._OVERLAY_LEASE_DEFAULT_MS:
    # production arms the timer by reading that same constant, so comparing
    # the two sides only ever proves the CALL SITE still names it. Changing
    # the constant's VALUE to Qt's ceiling (_QT_TIMER_MAX_INTERVAL_MS, 24.8
    # days) would arm the fake with the new number and leave a
    # constant-to-constant assertion green, which is exactly the lease that
    # never removes anything. The sibling pin at journey 4 (the clear-ack
    # deadline) already uses a literal for this reason.
    assert display.gui._overlay_lease_pair == (session_id, generation)
    assert display.lease_timer.interval_ms == 90_000
    # And the value has to keep working as a lease: Logic renews on the
    # keepalive tick (default 15 s), so an interval below that would fire
    # under a healthy Logic, and one measured in days would never fire at
    # all. Both ends are stated so a future retune has to face them.
    assert 60_000 <= display.lease_timer.interval_ms <= 600_000
    assert display.lease_timer.running

    acks = await _drain_expecting(
        display.commands_to_logic_queue,
        why="the display never reported the paint back to Logic",
    )
    assert len(acks) == 1, f"expected exactly one ack, got {acks!r}"
    ack = acks[0]
    assert ack["action"] == "overlay_state_changed"
    assert ack["state"] == "painted"
    assert ack["overlay_session_id"] == session_id
    assert ack["paint_generation"] == generation
    assert ack["monitor_ids"] == [_FAKE_HMONITOR]
    assert ack["snapshot_id"] == snapshot_id

    _feed_gui_commands_to_logic(controller, acks)
    await _await_true(
        lambda: controller.click_overlay_state.state is OverlayState.PAINTED,
        why="Logic never reached painted after the display acked the paint",
    )
    return session_id, generation


# ---------------------------------------------------------------------------
# Journey 4 -- the clear to the display process is lost
# ---------------------------------------------------------------------------


async def test_journey_4_a_lost_clear_lets_the_badge_lease_remove_the_numbers(
    journey_rig, display_rig,
):
    """Journey 4: the numbers must not survive a clear that never arrives.

    Badges 3 and 4 are on the screen. The user says "hide numbers". Logic
    dispatches the clear, closes its machine at once (the r2.4 immediate
    close), and the message never reaches the display process -- a Full
    queue drops it with a warning, or the display is wedged. Nothing on the
    Logic side will try again: the clear-ack watchdog is report-only by
    design (main.py:6837-6850), and the GUI-side badge lease is the remover
    of last resort (wh-overlay-slow-uia-stale-badges.9, merged as child .9).

    So this journey pins three things, in the order the user meets them:

      1. The badges really are still on the screen after the loss, while
         Logic already believes them gone. That gap is the defect; the
         journey asserts it exists rather than pretending it does not.
      2. Logic REPORTS it. The clear-ack watchdog fires and the fault is
         recorded for the pair, which is what raises the popup.
      3. The display removes the numbers by itself when the lease runs out,
         and tells Logic it did.

    Nothing here reads a pixel. The assertions are the manager's own record
    of which badge windows exist and the ``overlay_state_changed`` dicts
    that crossed the real queue.
    """

    import gui as gui_module
    import services.wheelhouse.main as main_module

    controller = journey_rig.controller
    manager = display_rig.manager
    # The two-module trap this file already documents for utils.system: if
    # the controller's class came from a different module object than the
    # one patched below, the deadline patch would silently miss.
    assert type(controller).__module__ == main_module.__name__
    assert type(display_rig.gui).__module__ == gui_module.__name__

    session_id, generation = await _badges_onto_the_display(
        journey_rig, display_rig, "snap-journey-4", 3, 4,
    )

    # --- the user says "hide numbers" --------------------------------------
    # The shipped deadline is 5 s against a 30 s per-test cap, with two more
    # waits after it; inject a short one rather than spend the budget.
    assert main_module._OVERLAY_CLEAR_ACK_DEADLINE_MS == 5000
    with patch.object(main_module, "_OVERLAY_CLEAR_ACK_DEADLINE_MS", 40):
        await controller.handle_overlay_command("hide", trace_id="trace-j4-hide")
        await _await_true(
            lambda: controller._overlay_pending_clear == (session_id, generation),
            why="Logic never handed the dispatched clear to its ack watchdog",
        )

        # --- the clear is LOST ---------------------------------------------
        # Taken off the queue and never delivered. Reading it first is the
        # point: the message Logic produced was correct and complete, and it
        # still never reached the screen.
        from services.wheelhouse.shared.clear_overlay import ClearOverlayEvent

        actions = await _drain_expecting(
            journey_rig.state_to_gui_queue,
            why="Logic never put a clear_overlay action on the GUI queue",
        )
        clears = [a for a in actions if a.get("action") == "clear_overlay"]
        assert len(clears) == 1, f"expected exactly one clear, got {actions!r}"
        lost = ClearOverlayEvent.from_dict(clears[0])
        assert (lost.overlay_session_id, lost.paint_generation) == (
            session_id, generation,
        )

        # ASSERTION 1: the numbers are still on the screen, and the display
        # still holds a lease over them -- while Logic has already closed.
        assert _badge_window_monitors(manager) == [_FAKE_HMONITOR]
        assert display_rig.gui._overlay_lease_pair == (session_id, generation)
        assert controller.click_overlay_state.state is OverlayState.CLOSED

        # ASSERTION 2: Logic reports the loss. _report_overlay_clear_fault
        # records the pair and logs at ERROR, which is what the error
        # notifier turns into a user-visible popup -- independent of the GUI
        # queue that just swallowed the clear.
        await _await_true(
            lambda: controller._overlay_clear_fault_reported_pair == (
                session_id, generation,
            ),
            why=(
                "the clear-ack watchdog never reported the lost clear; the "
                "user would be left with numbers nothing admits to"
            ),
        )

    # --- the badge lease runs out ------------------------------------------
    display_rig.lease_timer.fire()

    # ASSERTION 3: no badge window is left, and no teardown debt behind it.
    assert _badge_window_monitors(manager) == []
    assert manager._pending_destroy == []
    assert not manager.teardown_pending
    assert not manager.has_deferred_teardown_ack
    # Nothing is left to lease, and the 2 s teardown retry has nothing to do.
    assert display_rig.gui._overlay_lease_pair is None
    assert not display_rig.lease_timer.running
    assert not display_rig.teardown_timer.running

    # ASSERTION 4: the display told Logic, at the pair it had painted.
    expiries = await _drain_expecting(
        display_rig.commands_to_logic_queue,
        why="the display never reported the lease expiry back to Logic",
    )
    assert len(expiries) == 1, f"expected one expiry, got {expiries!r}"
    expired = expiries[0]
    assert expired["action"] == "overlay_state_changed"
    assert expired["state"] == "expired"
    assert expired["overlay_session_id"] == session_id
    assert expired["paint_generation"] == generation

    # And Logic takes it as bookkeeping: the machine is already closed, and
    # the expiry does not reopen anything or report a second fault.
    _feed_gui_commands_to_logic(controller, expiries)
    await _drain_gui_queue(journey_rig.state_to_gui_queue, settle_s=0.1)
    assert controller.click_overlay_state.state is OverlayState.CLOSED
    assert controller._overlay_clear_fault_reported_pair == (
        session_id, generation,
    )


# ---------------------------------------------------------------------------
# Journey 6 -- a process dies at each step, and the application restarts
# ---------------------------------------------------------------------------


async def _reach_death_step(rig: JourneyRig, display: DisplayRig, step: str):
    """Drive the overlay to ``step`` and hand back the pair on the screen.

    Every step starts from badges the display really painted, then a REFRESH
    -- a second "show numbers" over painted badges -- which bumps the paint
    generation to 1. The refresh is not decoration: a restarted Logic numbers
    its pairs from ``(1, 0)`` again (click_overlay_state.py:549-559), so a
    display whose generation gate still held a (1, 1) high-water mark would
    refuse every paint the new Logic can ever produce. Painting once at
    (1, 0) would leave a gate that survived the restart indistinguishable
    from one that was reset.
    """

    session_id, generation = await _badges_onto_the_display(
        rig, display, "snap-journey-6", 3, 4,
    )
    assert (session_id, generation) == (1, 0)
    session_id, generation = await _badges_onto_the_display(
        rig, display, "snap-journey-6", 3, 4,
    )
    assert (session_id, generation) == (1, 1), (
        "the refresh did not bump the paint generation; the gate assertion "
        "after the restart would prove nothing"
    )

    controller = rig.controller
    if step == "badges_painted":
        # Nothing is in flight: the numbers are simply up, and the user is
        # reading them when the process dies.
        assert controller._overlay_click_in_flight is None
        assert controller._overlay_pending_clear is None
        return session_id, generation

    if step == "click_in_flight":
        # Everything the two paints themselves sent to Input, off the buffer
        # first, so the next frame read is unambiguously the click's. Both
        # actions are snapshot-store bookkeeping the refresh generated: the
        # immediate keepalive tick, and the unpin of the generation the
        # refresh superseded (click_overlay_state.py:1736).
        outstanding = await _drain_command_frames(rig)
        assert {f[0] for f in outstanding} <= {
            "refresh_overlay_snapshot", "unpin_snapshot",
        }, f"unexpected commands already queued for Input: {outstanding!r}"

        # "x-ray click three". The command is framed into shared memory and
        # Logic is awaiting the reply Input will never send.
        await controller.forward_click_element(
            _speak_click("three"), "trace-j6-click",
        )
        await _await_frame(rig.command_ready_event)
        action, params, _has, _rid, _tid = _read_command_frame(rig.shm)
        assert action == "click_snapshot_item"
        assert params["item_id"] == "snap-journey-6-item-3"
        assert controller._overlay_click_in_flight == (
            "snap-journey-6", "snap-journey-6-item-3", "trace-j6-click",
        )
        return session_id, generation

    assert step == "clear_in_flight", step
    # "hide numbers". Logic dispatched the clear onto the GUI queue and armed
    # the ack watchdog; the display has not been pumped, so the clear is
    # still queued and the badges are still up.
    await controller.handle_overlay_command("hide", trace_id="trace-j6-hide")
    # Ten seconds, not the three-second default, and the reason is production
    # ordering rather than slowness. ``_dispatch_overlay_effects`` holds
    # ``_overlay_effect_lock`` for a WHOLE batch (main.py:6090-6094), and the
    # refresh above left an unpin batch inside it -- a ``send_request`` to an
    # Input process that does not exist in this journey, which runs its full
    # ``response_timeout_ms`` (3000 ms) before timing out
    # (main.py:7540-7547). The hide's clear batch is FIFO behind it, so a
    # three-second wait sits exactly on that boundary and fails
    # intermittently. Nothing is slept: this returns the moment the lock
    # frees.
    await _await_true(
        lambda: controller._overlay_pending_clear == (session_id, generation),
        timeout_s=10.0,
        why="Logic never handed the dispatched clear to its ack watchdog",
    )
    return session_id, generation


async def _drain_command_frames(
    rig: JourneyRig, *, settle_s: float = 0.15,
) -> list:
    """Consume the frames already waiting for Input, as the Input loop does.

    Production sends Input more than a journey's own utterance: reaching
    ``painted`` with a pinned snapshot arms the snapshot keepalive, and its
    FIRST tick is a ``call_soon`` rather than a full interval
    (main.py:8331-8344), so a ``refresh_overlay_snapshot`` frame is already
    in the buffer by the time the journey speaks again. Copy-then-clear is
    what the Input command loop does with a frame (input_proc.py:1066), and
    clearing the event is what frees the buffer for the next one.

    Returns the frames consumed, so a caller can assert WHICH commands were
    already outstanding rather than discarding them unseen.
    """

    drained = []
    deadline = time.monotonic() + settle_s
    while time.monotonic() < deadline:
        if rig.command_ready_event.is_set():
            drained.append(_read_command_frame(rig.shm))
            rig.command_ready_event.clear()
            deadline = time.monotonic() + settle_s
        await asyncio.sleep(0.005)
    return drained


@pytest.mark.parametrize("dead_process", ["logic", "input", "display"])
@pytest.mark.parametrize(
    "death_step", ["badges_painted", "click_in_flight", "clear_in_flight"],
)
async def test_journey_6_a_crash_at_any_step_leaves_no_numbers_and_no_half_finished_intent(
    journey_rig, display_rig, dead_process, death_step,
):
    """Journey 6: whichever process dies, whenever, recovery is the same.

    HOW A DEATH IS MODELLED, and why that is the real behaviour. The
    launcher's supervision loop (launcher.py:850-866) polls
    ``is_alive()`` on all three children; the instant ANY of them is gone
    it logs which, sets the SHARED ``shutdown_event``, and breaks. The
    identity of the dead process changes the log line and nothing else
    that the survivors can observe -- they see one event. So the death is
    modelled by setting that event, and ``dead_process`` decides only what
    happens to the badge WINDOWS, which belong to the display process:

      * ``display`` -- the windows died with the process that owned them,
        and the launcher spawns a fresh display process (launcher.py:820).
      * ``logic`` / ``input`` -- the display process is the survivor here,
        so its badge windows are still on the screen with nothing left
        alive that put them there. In the shipped launcher the supervisor
        kills the display too, which is strictly easier (a dead process
        cannot leave a window up); this journey tests the harder case,
        which is the one ``_send_overlay_startup_reset`` was written for
        (main.py:7356-7377 names it).

    HOW A RESTART IS MODELLED. The same supervisor loop iterates
    (launcher.py:789): a NEW ``SharedMemory`` block with ``create=True``
    (:794 and :798), new queues, new events, and all three processes
    spawned onto them. So the restarted Logic here is a second real
    ``LogicController`` over a second set of real transport objects. It
    reads the SAME user-data directory the dead one did, because a
    restart does not get a clean disk -- what must not survive is the
    in-memory intent, and none of it is written down.

    WHAT THE JOURNEY PINS:

      1. At the moment of death the numbers really ARE still on the screen,
         and the display applies nothing further -- its queue poll
         short-circuits on the shutdown event (gui.py:1614-1616). Whatever
         Logic had queued dies queued.
      2. Logic reports no fault for an intentional exit. The clear-ack
         deadline callback can already be sitting on the loop when shutdown
         lands, and its backstop (main.py:6943-6945) is what keeps a crash
         from also popping an overlay error at the user.
      3. No half-finished intent crosses the restart: no click awaiting a
         reply, no clear awaiting an ack, no pinned snapshot, no session.
      4. No numbers are left on the screen once the restarted Logic
         announces itself.
      5. The restarted Logic can paint again -- which needs the display's
         generation gate to have been RESET, because a from-zero pair is
         strictly older than the mark the dead incarnation left.
    """

    rig = journey_rig
    display = display_rig
    controller = rig.controller
    session_id, generation = await _reach_death_step(rig, display, death_step)

    # --- a process dies ----------------------------------------------------
    # ``_shutdown_gui`` is replaced by a recorder: the real one calls
    # ``QApplication.instance().quit()`` (gui.py:3782-3784) on the QApplication
    # the whole test session shares. Recording the call is also the assertion
    # -- reaching it is the only way out of _check_queues_and_events once the
    # shutdown event is set.
    shutdown_gui_calls = []
    display.gui._shutdown_gui = lambda: shutdown_gui_calls.append("called")
    assert display.gui.shutdown_event is controller.shutdown_event, (
        "the display rig is not wired to the Logic rig's shutdown event, so "
        "setting it would not model a launcher-signalled death"
    )
    controller.shutdown_event.set()

    display.gui._check_queues_and_events()

    # ASSERTION 1: the display stopped consuming, and the numbers are still up.
    assert shutdown_gui_calls == ["called"]
    assert _badge_window_monitors(display.manager) == [_FAKE_HMONITOR], (
        "the badge windows vanished on their own; then nothing about the "
        "restart could be tested, and the shipped GUI has no such path"
    )

    if death_step == "clear_in_flight":
        # The clear Logic dispatched is STILL on the queue: the poll returned
        # before the drain. Nothing will re-send it -- the sender is gone.
        leftover = await _drain_expecting(
            rig.state_to_gui_queue,
            why="the clear Logic dispatched is not on the GUI queue at all",
        )
        clears = [a for a in leftover if a.get("action") == "clear_overlay"]
        assert len(clears) == 1, f"expected one queued clear, got {leftover!r}"

        # ASSERTION 2: an intentional exit is not an overlay fault. The
        # deadline callback is invoked the way loop.call_later would invoke
        # it (main.py:6880-6882), with shutdown already set.
        controller._fire_overlay_clear_ack_deadline(
            session_id, generation, "trace-j6-hide",
        )
        assert controller._overlay_pending_clear is None
        assert controller._overlay_clear_fault_reported_pair is None, (
            "a crash popped an overlay clear-fault error at the user; the "
            "application is exiting on purpose and there is nothing to fix"
        )

    # --- the launcher restarts the application -----------------------------
    # Same user-data directory, new everything else: a restart re-reads the
    # disk it always read, and gets fresh transport (launcher.py:794-805).
    async with _real_logic_controller(rig.data_dir) as restarted:
        new_controller = restarted.controller
        assert restarted.shm.name != rig.shm.name, (
            "the restart reused the dead incarnation's shared-memory block; "
            "launcher.py:794 creates a fresh one every iteration"
        )
        assert not restarted.command_ready_event.is_set(), (
            "a command frame was pending on a freshly created buffer"
        )
        assert new_controller.shutdown_event is not controller.shutdown_event
        assert not new_controller.shutdown_event.is_set()

        # ASSERTION 3: no half-finished intent crossed the restart.
        machine = new_controller.click_overlay_state
        assert machine.state is OverlayState.CLOSED
        assert machine.pinned_snapshot_id is None
        assert new_controller._overlay_click_in_flight is None
        assert new_controller._overlay_pending_clear is None
        assert new_controller._overlay_clear_fault_reported_pair is None
        assert new_controller._overlay_visible_painted_pair is None

        # --- the display side of the restart -------------------------------
        with ExitStack() as stack:
            if dead_process == "display":
                # A window belongs to the process that created it, so nothing
                # the dead display painted can outlive it. The launcher spawns
                # a new display process onto the new queues.
                display.manager.clear_all()
                fresh_commands = MpQueue()
                stack.callback(fresh_commands.close)
                survivor = stack.enter_context(
                    _real_display_process(
                        restarted.state_to_gui_queue,
                        fresh_commands,
                        new_controller.shutdown_event,
                        monitors=[_fake_monitor()],
                    )
                )
                assert _badge_window_monitors(survivor.manager) == []
            else:
                # The display process survived, so its badge windows are still
                # on the screen with nothing alive that put them there. The
                # launcher handed the restarted processes fresh queues and a
                # fresh shutdown event (launcher.py:800-805), which is what
                # these two rebinds model.
                display.gui.state_from_logic_queue = restarted.state_to_gui_queue
                display.gui.shutdown_event = new_controller.shutdown_event
                survivor = display
                assert _badge_window_monitors(survivor.manager) == [
                    _FAKE_HMONITOR
                ]
                assert survivor.gui._overlay_lease_pair == (
                    session_id, generation,
                )

            # The restarted Logic announces itself, once, at startup
            # (main.py:11157 -> _send_overlay_startup_reset).
            new_controller._send_overlay_startup_reset()

            # ASSERTION 4: no numbers are left on the screen. For a display
            # that died this is already true before the pump -- the delivery
            # of the reset is proved instead by assertion 5, which rides the
            # same FIFO queue behind it.
            await _pump_display(
                survivor,
                until=lambda: _badge_window_monitors(survivor.manager) == [],
                why=(
                    "the numbers were still on the screen after the "
                    "application restarted"
                ),
            )
            assert survivor.gui._overlay_lease_pair is None
            assert survivor.manager._pending_destroy == []
            assert not survivor.manager.teardown_pending
            assert not survivor.manager.has_deferred_teardown_ack
            assert not survivor.lease_timer.running
            assert not survivor.teardown_timer.running
            # The reset reports nothing back: there is no live pair to report
            # on and the new Logic starts from closed (gui.py:2247-2254).
            silence = await _drain_gui_queue(
                survivor.commands_to_logic_queue, settle_s=0.1,
            )
            assert silence == [], (
                f"reset_overlay reported {silence!r} to a Logic process that "
                "has no overlay session to attach it to"
            )

            # ASSERTION 5: the restarted Logic can paint again. Its first pair
            # is (1, 0) -- strictly OLDER than the (1, 1) the dead incarnation
            # left on the display's generation gate -- so this paint can only
            # land if the gate was reset, not merely emptied of windows.
            new_sid, new_gen, new_effect = _paint_badges_awaiting_display(
                new_controller, "snap-journey-6-restarted", 5,
            )
            assert (new_sid, new_gen) == (1, 0)
            assert (new_sid, new_gen) < (session_id, generation), (
                "the restarted Logic did not renumber from zero, so this "
                "journey is no longer testing the stale-gate case"
            )
            await new_controller._overlay_dispatch_paint(
                new_effect, "trace-j6-restart",
            )
            await _pump_display(
                survivor,
                until=lambda: survivor.gui._overlay_lease_pair == (
                    new_sid, new_gen,
                ),
                why=(
                    "the restarted Logic's paint never reached the screen; a "
                    "generation gate carrying the dead incarnation's "
                    "high-water mark refuses every pair a from-zero Logic "
                    "can produce, and the user never sees numbers again"
                ),
            )
            assert _badge_window_monitors(survivor.manager) == [_FAKE_HMONITOR]
            acks = await _drain_expecting(
                survivor.commands_to_logic_queue,
                why="the display never reported the restarted paint to Logic",
            )
            assert len(acks) == 1, f"expected one ack, got {acks!r}"
            assert acks[0]["action"] == "overlay_state_changed"
            assert acks[0]["state"] == "painted"
            assert acks[0]["overlay_session_id"] == new_sid
            assert acks[0]["paint_generation"] == new_gen
            assert acks[0]["snapshot_id"] == "snap-journey-6-restarted"

            if dead_process != "display":
                # Leave the survivor pointed back at its own rig's transport,
                # so the display_rig fixture tears down what it created.
                display.gui.state_from_logic_queue = rig.state_to_gui_queue
                display.gui.shutdown_event = controller.shutdown_event


# ---------------------------------------------------------------------------
# Answering the Input side over the real transport
# ---------------------------------------------------------------------------


class _ForegroundSampler:
    """A stand-in for ``_capture_overlay_foreground_identity`` (main.py:8731).

    The real method reads THIS MACHINE's foreground window -- it calls
    ``win32gui.GetForegroundWindow()`` (main.py:8753) and resolves the pid,
    process name and creation time from it. A journey that let it run would
    compare a crafted reply against whatever window the person running the
    suite happens to have in front, so the verdict would be theirs and not the
    code's. That makes the patch a hermeticity requirement, not a convenience.

    It COUNTS its calls as well as answering them, so a journey can prove the
    fence really sampled through this seam rather than assert that it did: a
    fence that never sampled would leave ``calls`` at zero while still
    reaching whatever verdict the journey expected.
    """

    def __init__(self, identity) -> None:
        self.identity = identity
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.identity


# The two windows journey 3b is about. They share a pid and a process name and
# differ in ``hwnd`` and ``window_creation_time`` on purpose: that is the shape
# of "the click opened a dialog in the same application". ``identity_matches``
# (overlay_focus_hooks.py:176-181) compares all four fields.
_WINDOW_THE_USER_CLICKED_IN = ForegroundIdentity(
    hwnd=0x4A01,
    pid=4101,
    process_name="journeyapp.exe",
    window_creation_time=1_700_000_000_000,
)
_THE_DIALOG_THE_CLICK_OPENED = ForegroundIdentity(
    hwnd=0x4A02,
    pid=4101,
    process_name="journeyapp.exe",
    window_creation_time=1_700_000_005_000,
)


async def _answer_input_request(rig: JourneyRig, *, expect_action, build_reply):
    """Read one Logic-to-Input command and answer it through the REAL demuxer.

    The reply is not handed to ``send_request``'s Future directly and
    ``send_request`` is not stubbed. It goes onto ``app.response_queue``
    (app.py:178) -- the same real ``multiprocessing.Queue`` the Input process
    writes to -- and the real demuxer task the rig started
    (``_start_demuxer``, app.py:298) pops it, matches ``request_id`` against
    ``response_futures`` (app.py:361-362) and resolves the awaiting Future. So
    the request id, the demux and the schema parse are all production's.

    ``build_reply`` receives the params dict Logic actually sent, so a reply
    can echo the pair and the trace id verbatim the way the Input handler
    does. Returns those params.
    """

    await _await_frame(rig.command_ready_event)
    action, params, has_params, request_id, trace_id = _read_command_frame(
        rig.shm,
    )
    # Copy-then-clear, exactly as the Input loop does (input_proc.py:1067,
    # ``command_ready_event.clear()``). Without the clear, the next command
    # Logic sends is dropped as ``unread_frame`` (app.py:764-773) and a later
    # assertion about Logic would really be about transport backpressure.
    rig.command_ready_event.clear()
    assert action == expect_action, (
        f"expected the {expect_action!r} command, got {action!r} "
        f"params={params!r}"
    )
    assert has_params, f"{action} carried no params"
    assert request_id is not None, (
        f"{action} went out fire-and-forget, so nothing can answer it"
    )
    reply = dict(build_reply(params))
    # The two envelope keys the Input side adds before it puts a handler-owned
    # Schema A response on the queue (the ``_HANDLES_OWN_RESPONSE`` machinery
    # in input_proc.py); the demuxer keys on request_id.
    reply["request_id"] = request_id
    reply["action"] = action
    rig.app.response_queue.put(reply)
    return params


def _walk_reply(params, *, snapshot_id, summary, read_from) -> dict:
    """The wire dict a ``start_overlay_walk`` handler answers with.

    Built through the production schema (``StartOverlayWalkResponse.to_dict``)
    rather than hand-written, so a journey cannot craft a payload the real
    handler could never emit: ``from_dict`` enforces five cross-field
    invariants (shared/start_overlay_walk.py ``_validate_cross_field``) and
    Logic parses this dict with exactly that classmethod.

    The four ``foreground_*`` fields are the window the walk says it READ
    (wh-overlay-slow-uia-stale-badges.2.2.1), which is the value the commit
    fence checks.
    """

    from services.wheelhouse.shared.start_overlay_walk import (
        StartOverlayWalkResponse,
    )

    return StartOverlayWalkResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=summary,
        trace_id=params["trace_id"],
        overlay_session_id=params["overlay_session_id"],
        paint_generation=params["paint_generation"],
        foreground_window=read_from.hwnd,
        foreground_pid=read_from.pid,
        foreground_process_name=read_from.process_name,
        foreground_window_creation_time=read_from.window_creation_time,
    ).to_dict()


# ---------------------------------------------------------------------------
# Journey 3, half B -- the settle reply describes a window the user has left
# ---------------------------------------------------------------------------
#
# Half A pinned the state machine's half: a focus change does NOT cancel the
# settling read (click_overlay_state.py:1301-1307 returns NO_OP). This half
# pins where the refusal actually lives -- the INTEGRATION's commit fence in
# ``_overlay_dispatch_build`` (main.py:6148). The settle leg is
# main.py:6355-6396: ``if build_ok and is_settle:`` then
# ``if not self._overlay_settle_read_is_current(reported, trace_id=...)``,
# which on refusal clears the walking cue, sets ``build_ok = False`` and
# ``snapshot_id = None``, and on acceptance stores
# ``self._overlay_settle_read_identity = (snapshot_id, reported)``.
#
# TWO tests, not one with two sections, for the reason the three config parts
# are three tests: a refusal-only test would pass against a fence that refused
# EVERYTHING, and the mutation gate's output has to be able to say which half
# a mutation reached.


@dataclass
class _SettleOutcome:
    """What one settle round trip did, captured before the rig is torn down."""

    sent: dict
    actions: list
    machine_state: Any
    pinned_snapshot_id: Any
    settle_read_identity: Any
    foreground_samples: int
    session_id: int
    settle_generation: int


async def _drive_journey_3b(tmp_path: Path, *, read_from) -> _SettleOutcome:
    """Run one post-click settle round trip and report what the fence did.

    The ONLY difference between the two journeys below is ``read_from`` -- the
    window the crafted reply says it was read from -- so the shared driver is
    what makes the pair a controlled comparison rather than two tests that
    happen to differ somewhere unstated. The current foreground is always
    ``_THE_DIALOG_THE_CLICK_OPENED``: the click opened a dialog, which is the
    common case for "click N" and the reason the machine does not cancel.
    """

    from services.wheelhouse.click_overlay_state import BuildReason, EffectKind

    data_dir = tmp_path / "journey-3b-data"
    data_dir.mkdir()
    async with _real_logic_controller(
        data_dir, config_path=_write_settling_config(tmp_path),
    ) as rig:
        controller = rig.controller
        machine = controller.click_overlay_state
        assert machine.settle_after_click, (
            "the real factory did not carry [click] overlay_settle_after_click "
            "onto the machine; with the flag off main.py:4270 makes the whole "
            "settle path inert and this journey would pin nothing"
        )
        sampler = _ForegroundSampler(_THE_DIALOG_THE_CLICK_OPENED)
        with patch.object(
            controller, "_capture_overlay_foreground_identity", sampler,
        ):
            session_id, generation = _paint_badges(
                controller, "snap-journey-3b", 3, 4,
            )
            # Anything the rig's own construction queued is not this journey.
            await _drain_gui_queue(rig.state_to_gui_queue, settle_s=0.1)

            # --- the click succeeds; the machine settles --------------------
            completed = machine.apply(
                OverlayEvent(OverlayEventKind.CLICK_COMPLETE)
            )
            assert machine.state is OverlayState.POST_CLICK_SETTLING
            settle_build = next(
                e for e in completed.effects
                if e.kind is EffectKind.DISPATCH_BUILD
            )
            assert settle_build.build_reason is BuildReason.SETTLE
            settle_generation = machine.paint_generation
            assert settle_generation == generation + 1

            # --- the real performer, over the real transport ----------------
            build_task = asyncio.ensure_future(
                controller._overlay_dispatch_build(settle_build, "trace-j3b")
            )
            try:
                sent = await _answer_input_request(
                    rig,
                    expect_action="start_overlay_walk",
                    build_reply=lambda params: _walk_reply(
                        params,
                        snapshot_id="snap-journey-3b",
                        summary=_summary("snap-journey-3b", 3, 4),
                        read_from=read_from,
                    ),
                )
                await build_task
            finally:
                if not build_task.done():
                    build_task.cancel()
                    await asyncio.gather(build_task, return_exceptions=True)

            # The commit feed is deferred through ``loop.call_soon`` (the
            # ``_feed`` closure in main.py), and the effects it schedules run
            # as their own task, so the drain below is what lets both run.
            actions = await _drain_gui_queue(
                rig.state_to_gui_queue, settle_s=0.5,
            )
            return _SettleOutcome(
                sent=sent,
                actions=actions,
                machine_state=machine.state,
                pinned_snapshot_id=machine.pinned_snapshot_id,
                settle_read_identity=controller._overlay_settle_read_identity,
                foreground_samples=sampler.calls,
                session_id=session_id,
                settle_generation=settle_generation,
            )


def _assert_the_settle_read_really_went_out(outcome: _SettleOutcome) -> None:
    """Both halves share this: the build Logic sent WAS the settle read.

    Without it a journey could pass on a build that was never a settle at all
    -- and ``is_settle`` (main.py:6262) is what arms the whole fence, so a
    non-settle build would sail through it for a reason that has nothing to do
    with the window identity.
    """

    assert outcome.sent["scope"] == "focused_window"
    assert outcome.sent["settle"] is True, (
        "the settle flag was not on the wire, so the Input handler would walk "
        "once instead of reading through the settle detector (main.py:6218)"
    )
    assert outcome.sent["compare_snapshot_id"] == "snap-journey-3b", (
        "the read is compared against the pin that was on screen when the "
        "user clicked (main.py:6219)"
    )
    assert outcome.sent["overlay_session_id"] == outcome.session_id
    assert outcome.sent["paint_generation"] == outcome.settle_generation
    assert outcome.foreground_samples >= 1, (
        "the fence never sampled the current foreground, so whatever verdict "
        "it reached was not reached by comparing windows (the "
        "_capture_overlay_foreground_identity call at main.py:8671)"
    )


async def test_journey_3b_a_settle_read_from_a_window_the_user_left_never_paints(
    tmp_path,
):
    """Journey 3, half B: the fence refuses a reply read from the old window.

    The user says "click 3", the click succeeds and opens a dialog. The
    machine enters ``post_click_settling`` and dispatches the settle read, and
    it deliberately does NOT cancel when the dialog takes the foreground (half
    A). The read Input answers with therefore describes the window the user
    has ALREADY LEFT.

    Painting it would put window A's badges over window B, and a number spoken
    against them would then resolve against a list Input itself rejects. The
    commit fence refuses instead, and it fails CLOSED: build_ok becomes False,
    which ``_on_post_click_settling`` answers by unpinning and closing
    (click_overlay_state.py:1245-1256), so no pin is left behind for a window
    that is not in front.
    """

    outcome = await _drive_journey_3b(
        tmp_path, read_from=_WINDOW_THE_USER_CLICKED_IN,
    )
    _assert_the_settle_read_really_went_out(outcome)

    # ASSERTION 1: nothing was painted. This is the whole journey -- the
    # badges for window A never reach the display process.
    paints = [a for a in outcome.actions if a.get("action") == "paint_overlay"]
    assert paints == [], (
        "a settle reply read from the window the user had already left was "
        f"painted anyway: {paints!r}"
    )

    # ASSERTION 2: no identity was carried to a pin. The acceptance leg is the
    # only writer of this slot (main.py:6393), so a value here would mean the
    # fence took the accepting branch.
    assert outcome.settle_read_identity is None

    # ASSERTION 3: the machine did not end up presenting the wrong window's
    # numbers. Fail-closed means CLOSED, with the pre-click pin given up.
    assert outcome.machine_state is OverlayState.CLOSED
    assert outcome.machine_state is not OverlayState.PAINTED
    assert not outcome.pinned_snapshot_id, (
        "the pre-click snapshot is still pinned after the refusal; a pin for "
        "a window that is not in front is what the fail-closed leg exists to "
        "avoid"
    )


async def test_journey_3b_a_settle_read_from_the_window_still_in_front_paints(
    tmp_path,
):
    """Journey 3, half B, the other half: a matching window IS painted.

    Without this the refusal above proves nothing -- a fence that refused
    every settle reply would pass it. Here the reply names the window that is
    still in front, so all four identity fields match
    (overlay_focus_hooks.py:176-181), the fence accepts, and the numbers come
    back.

    The reply carries the SAME snapshot id the machine already has pinned,
    which is how Input says "nothing changed" (click_overlay_state.py:1257).
    That leg repaints the pinned snapshot and emits no PIN_SNAPSHOT effect, so
    the identity the fence stored is still in its slot at the end -- which is
    what makes it observable here.
    """

    outcome = await _drive_journey_3b(
        tmp_path, read_from=_THE_DIALOG_THE_CLICK_OPENED,
    )
    _assert_the_settle_read_really_went_out(outcome)

    # ASSERTION 1: the numbers were painted, at the settle generation.
    paints = [a for a in outcome.actions if a.get("action") == "paint_overlay"]
    assert len(paints) == 1, (
        f"expected exactly one paint_overlay, got {outcome.actions!r}"
    )
    painted = paints[0]
    assert painted["overlay_session_id"] == outcome.session_id
    assert painted["paint_generation"] == outcome.settle_generation
    assert painted["snapshot_id"] == "snap-journey-3b"
    assert sorted(i["display_number"] for i in painted["items"]) == [3, 4]

    # ASSERTION 2: the READ's window was carried to the pin rather than
    # re-sampled (main.py:6393, consumed at main.py:7443-7450).
    assert outcome.settle_read_identity == (
        "snap-journey-3b", _THE_DIALOG_THE_CLICK_OPENED,
    )

    # ASSERTION 3: the machine committed the repaint rather than closing.
    assert outcome.machine_state is OverlayState.PAINT_IN_FLIGHT
    assert outcome.pinned_snapshot_id == "snap-journey-3b"


# ---------------------------------------------------------------------------
# Journey 2 -- the numbers repaint while the user is mid-sentence
# ---------------------------------------------------------------------------


def _pin_reply(params) -> dict:
    """The wire dict a ``pin_snapshot`` handler answers with."""

    from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=params["overlay_session_id"],
        snapshot_id=params["snapshot_id"],
        pinned=True,
    ).to_dict()


def _journey_2_after_summary() -> WalkSnapshotSummary:
    """The list the repaint puts up, with badge 2 on a DIFFERENT control.

    Badge 1 keeps its name, role AND bounds, so the guard has to
    discriminate rather than refuse the whole repaint. Badge 3 keeps its name
    and role but moves down the window, which is what an inserted row does to
    everything below it. Badge 2 changes all three, which is what a row
    insert or a content swap does to a positional number: ``uia_walker.py``
    increments ``display_number`` in walk order, so one control added
    anywhere renumbers everything after it.
    """

    def _item(number, name, top):
        return WalkSnapshotSummaryItem(
            item_id=f"snap-journey-2-after-item-{number}",
            display_number=number,
            name=name,
            role="Button",
            bounds=(0, top, 10, top + 10),
            monitor_id=0,
        )

    return WalkSnapshotSummary(
        snapshot_id="snap-journey-2-after",
        items=[
            _item(1, "control 1", 0),
            # Same badge number, different control, different place.
            _item(2, "Delete for ever", 40),
            _item(3, "control 3", 20),
        ],
        created_at_monotonic=2.0,
    )


@dataclass
class _Journey2Repaint:
    """What the shared journey-2 repaint leaves behind, for both halves."""

    controller: LogicController
    machine: object
    before: WalkSnapshotSummary
    after: WalkSnapshotSummary


@asynccontextmanager
async def _journey_2_repaint(journey_rig):
    """Drive the repaint both halves of journey 2 need, then hand it over.

    One driver, two tests, for the reason the refusal half cannot cover on
    its own. The refusal half is a STRICT expected failure, so every
    assertion inside it is absorbed: a child .4 that refused EVERY numbered
    click after a repaint would satisfy it, and the "badges 1 and 3 are
    unchanged" setup would be decoration. The safe half therefore has to be
    a separate test that is NOT expected to fail -- it passes today, must
    keep passing after .4, and goes red the moment the guard stops
    discriminating (review finding
    wh-overlay-slow-uia-stale-badges.12.1.3).

    The patch stays open across the yield: a numbered click can reach
    ``_capture_overlay_foreground_identity``, which reads this machine's
    real foreground window.
    """

    controller = journey_rig.controller
    machine = controller.click_overlay_state
    sampler = _ForegroundSampler(_WINDOW_THE_USER_CLICKED_IN)
    with patch.object(
        controller, "_capture_overlay_foreground_identity", sampler,
    ):
        session_id, generation = _paint_badges(
            controller, "snap-journey-2-before", 1, 2, 3,
        )
        await _drain_gui_queue(journey_rig.state_to_gui_queue, settle_s=0.1)

        # --- the numbers repaint while the user is mid-sentence -------------
        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.SHOW_NUMBERS),
            source="journey-2 repaint",
        )
        assert machine.state is OverlayState.REFRESH_IN_FLIGHT
        refresh_generation = machine.paint_generation
        assert refresh_generation == generation + 1

        await _answer_input_request(
            journey_rig,
            expect_action="start_overlay_walk",
            build_reply=lambda params: _walk_reply(
                params,
                snapshot_id="snap-journey-2-after",
                summary=_journey_2_after_summary(),
                read_from=_WINDOW_THE_USER_CLICKED_IN,
            ),
        )
        await _await_true(
            lambda: machine.pinned_snapshot_id == "snap-journey-2-after",
            why="the refresh build response never reached the machine",
        )
        await _answer_input_request(
            journey_rig, expect_action="pin_snapshot", build_reply=_pin_reply,
        )
        controller._apply_overlay_event(
            OverlayEvent(
                OverlayEventKind.PAINT_ACK,
                overlay_session_id=session_id,
                paint_generation=refresh_generation,
                paint_state=PaintAckState.PAINTED,
            ),
            source="journey-2 paint ack",
        )
        assert machine.state is OverlayState.PAINTED, (
            "the repaint never completed, so the journey would be about a "
            "refresh in flight rather than about numbers the user can see"
        )
        # The new list really is the one on screen, and badge 2 really did
        # change -- without this the refusal below could be right for the
        # wrong reason.
        assert machine.pinned_snapshot_id == "snap-journey-2-after"
        before = controller.click_snapshot_summary_cache.resolve(
            "snap-journey-2-before"
        ).summary
        after = controller.click_snapshot_summary_cache.resolve(
            "snap-journey-2-after"
        ).summary
        assert before is not None and after is not None
        from services.wheelhouse.speech.overlay_click_router import (
            renumber_click_is_safe,
        )
        assert not renumber_click_is_safe(before, after, 2), (
            "badge 2 is the same control before and after the repaint, so "
            "this journey is not describing the race it is named for"
        )
        assert renumber_click_is_safe(before, after, 1), (
            "badge 1 changed too, so a guard that refused everything would "
            "pass this journey"
        )

        # Everything the repaint itself sent to Input, off the buffer first,
        # so ASSERTION 1 is about the click and only the click. Both actions
        # are snapshot-store bookkeeping the repaint generated: the immediate
        # keepalive tick (main.py:8331-8344) and the unpin of the generation
        # the repaint superseded (click_overlay_state.py:1736). This is the
        # same drain-and-classify journey 6 does at ``click_in_flight``.
        #
        # A bare ``command_ready_event.clear()`` here is NOT enough, and the
        # difference is not cosmetic. ``_send_one`` PARKS an unsendable
        # payload on that event for up to ``_COMMAND_DELIVERY_TTL_S``
        # (app.py:753-774, 5.0s) and only then drops it as ``unread_frame``,
        # so the clear releases the parked ``unpin_snapshot`` frame straight
        # into the 0.3s window below. Measured 2026-08-29 with a frame
        # recorder over ``WheelHouseApp._frame_and_write``: with the bare
        # clear the only frames written were start_overlay_walk,
        # pin_snapshot, refresh_overlay_snapshot and unpin_snapshot -- no
        # click_snapshot_item frame was ever written, so ASSERTION 1 tripped
        # on bookkeeping while its message named the click. That made the
        # expected failure independent of the guard this journey is about:
        # the test would keep failing after child .4 lands, stay XFAIL, and
        # never fire the marker's delete-me signal.
        await _drain_gui_queue(journey_rig.state_to_gui_queue, settle_s=0.2)
        outstanding = await _drain_command_frames(journey_rig)
        assert {f[0] for f in outstanding} <= {
            "refresh_overlay_snapshot", "unpin_snapshot",
        }, f"unexpected commands already queued for Input: {outstanding!r}"

        yield _Journey2Repaint(
            controller=controller,
            machine=machine,
            before=before,
            after=after,
        )


async def test_journey_2_a_repaint_mid_sentence_refuses_the_spoken_number(
    journey_rig,
):
    """Journey 2: the numbers repaint while the user is still speaking.

    Badges 1, 2 and 3 are on screen. The user starts saying "click two". The
    window repaints before the words arrive, and badge 2 now names a
    different control in a different place. The number must be REFUSED with
    the "numbers just changed" notice, not resolved against a list the user
    never saw -- badge numbers are positional, so after a content change
    badge N keeping its meaning is the unlikely case.

    The repaint here is an ordinary refresh, not the browser timer's
    proactive one, and that is the whole point of the journey: child .4's
    acceptance is that the check no longer depends on the
    ``browser_processes`` list. This test was a strict expected failure until
    child .4 widened the arming from the proactive refresh alone to every
    repaint that replaces the pinned list; the marker was deleted then, as
    its own text instructed.

    Every step is production's. The refresh is fed through
    ``_apply_overlay_event`` -- the same entry the focus hooks and the build
    feed use -- the walk and pin replies cross the real transport and the
    real demuxer, and the spoken number enters at
    ``forward_click_element``.
    """

    from services.wheelhouse.speech.overlay_click_router import (
        OVERLAY_NUMBERS_CHANGED,
    )

    async with _journey_2_repaint(journey_rig) as repaint:
        controller = repaint.controller

        # --- the words the user began before the repaint arrive -------------
        await controller.forward_click_element(
            _speak_click("two"), "trace-journey-2",
        )

        # ASSERTION 1: no click reached Input.
        await _assert_no_further_frame(
            journey_rig,
            window_s=0.3,
            why=(
                "a click_snapshot_item frame reached Input for a number "
                "spoken against badges the repaint had already replaced"
            ),
        )

        # ASSERTION 2: the user was told, by name.
        actions = await _drain_gui_queue(journey_rig.state_to_gui_queue)
        notices = [
            a for a in actions if a.get("action") == "show_click_notice"
        ]
        assert len(notices) == 1, (
            f"expected exactly one click notice, got {actions!r}"
        )
        notice = notices[0]
        assert notice["outcome"] == "execution_failed"
        assert notice["reason"] == OVERLAY_NUMBERS_CHANGED
        assert notice["trace_id"] == "trace-journey-2"


async def test_journey_2_an_unchanged_badge_stays_clickable_across_that_repaint(
    journey_rig,
):
    """Journey 2, the other half: badge 1 did not change, so it still works.

    The refusal half above proves the changed badge is refused. On its own
    that is satisfied by a guard which refuses everything, and the user pays
    for it: a hands-free user looking at an unchanged badge 1 would be told
    the numbers just changed, with no way to click what is plainly still
    there. This half is what makes the pair discriminate.

    It is deliberately NOT an expected failure. Nothing blocks it -- the
    click dispatches today -- so it is a live regression guard for whoever
    lands child .4, and it is the only place in this file where a
    post-repaint SAFE number is driven through the real controller. Gate
    mutation M12 forces ``_overlay_renumber_click_safe`` to refuse
    everything and expects exactly this test to go red.
    """

    from services.wheelhouse.speech.overlay_click_router import (
        renumber_click_is_safe,
    )

    async with _journey_2_repaint(journey_rig) as repaint:
        # Same repaint, same moment, the other number.
        assert renumber_click_is_safe(repaint.before, repaint.after, 1)

        await repaint.controller.forward_click_element(
            _speak_click("one"), "trace-journey-2-safe",
        )

        await _await_frame(journey_rig.command_ready_event)
        action, params, has_params, request_id, trace_id = (
            _read_command_frame(journey_rig.shm)
        )
        assert action == "click_snapshot_item", (
            "an unchanged badge was not clicked after an ordinary repaint; a "
            f"guard that refuses every renumbered list sends this ({action!r} "
            "instead), and the user cannot click a number still on screen"
        )
        assert has_params
        # The AFTER list, not the one the repaint replaced: badge 1 keeps its
        # number, so a click resolved against the stale list would carry the
        # before-snapshot ids and still look like a success.
        assert params["snapshot_id"] == "snap-journey-2-after"
        assert params["item_id"] == "snap-journey-2-after-item-1"
        assert params["trace_id"] == "trace-journey-2-safe"
        assert trace_id == "trace-journey-2-safe"
        assert request_id is not None

        # And no refusal notice was shown for the click that went through.
        actions = await _drain_gui_queue(journey_rig.state_to_gui_queue)
        notices = [
            a for a in actions if a.get("action") == "show_click_notice"
        ]
        assert notices == [], (
            f"a click that reached Input also showed a notice: {notices!r}"
        )



# ---------------------------------------------------------------------------
# The speech side, for journeys 5 and 7
# ---------------------------------------------------------------------------


def _real_speech_processor(rig: JourneyRig, tmp_path: Path):
    """A real ``SpeechProcessor`` over the journey rig's real app.

    ``app`` and ``logic_controller`` are the rig's real ones, so a dictated
    word travels the production route -- ``_send_to_dictation`` ->
    ``app.send_request('intelligent_insert_text', ...)`` -> the real sender
    chain -> a real frame in real shared memory, which
    ``_read_command_frame`` reads back through production's
    ``_read_envelope``.

    The catalog is the REAL ``PatternCatalog`` over the tracked
    ``speech/config/patterns.toml``, so the routing decision is the shipped
    one rather than a fixture's opinion of it. Its second argument is a
    tmp_path file that does not exist: the constructor's own docstring says
    to "pass an explicit path in tests to stay hermetic", because the default
    resolves to ``get_user_data_dir()/user_patterns.toml`` -- this machine's
    editable overrides.

    ``text_parser`` is None on purpose, and it is never touched on this path.
    Measured 2026-08-29 with ``grep -n 'self\\.text_parser'
    speech/speech_processor.py``: eleven hits, one of them the constructor's
    own assignment, and the other ten all inside
    ``_fire_trailing_action_for_word``, ``_consume_pending_bare_number``,
    ``_execute_command``, ``_process_remainder`` and
    ``_find_earliest_replacement`` -- command and replacement execution, none
    of which a plain dictated word reaches. The "gate released" half of
    journey 5 is the running proof: it gets its ``intelligent_insert_text``
    frame with this argument None.

    ``focus_redirect_policy`` is left None so dictation cannot route to the
    persistent hidden editor and leave shared memory empty for a reason
    unrelated to the gate.
    """

    from speech.pattern_catalog import PatternCatalog
    from speech.speech_processor import SpeechProcessor

    catalog = PatternCatalog(
        str(_SERVICE_DIR / "speech" / "config" / "patterns.toml"),
        str(tmp_path / "no-user-patterns.toml"),
    )
    assert catalog.pattern_count > 0, (
        "the catalog loaded in degraded mode (pattern_count == 0), so the "
        "router would pass everything through and a refusal would prove "
        "nothing about the gate"
    )
    return SpeechProcessor(
        word_queue=asyncio.Queue(),
        catalog=catalog,
        text_parser=None,
        app=rig.app,
        logic_controller=rig.controller,
    )


# A word no shipped pattern claims, so the router's decision is DICTATE. It is
# checked rather than assumed: ``_dictate_and_collect_frames`` reports every
# action that crossed the boundary, and the "gate released" half of journey 5
# asserts that ``intelligent_insert_text`` is among them.
_A_DICTATED_WORD = "banana"


async def _dictate_and_collect_frames(
    rig: JourneyRig, processor, word: str, *, window_s: float = 0.6,
) -> list:
    """Feed one dictated word and report every action that reached Input.

    ``_send_to_dictation`` AWAITS its ``send_request``, and no journey answers
    it, so the call is run as a task and cancelled at the end rather than
    waited out for the app's 5 s response timeout. The frames are read and the
    ready event cleared the way the Input loop does, so several actions in one
    window are all visible instead of the first one blocking the rest
    (app.py:764-773).
    """

    from speech.word_event import WordEvent

    actions: list = []
    task = asyncio.ensure_future(
        processor.process_word_event(
            WordEvent(
                word=word, start_of_utterance=True, end_of_utterance=True,
            )
        )
    )
    try:
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            if rig.command_ready_event.is_set():
                actions.append(_read_command_frame(rig.shm)[0])
                rig.command_ready_event.clear()
            await asyncio.sleep(0.01)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return actions


# ---------------------------------------------------------------------------
# Journey 5 -- the read command is destroyed, and the gate must not stick
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Waiting on wh-overlay-slow-uia-stale-badges.7 (refuse dictation "
        "while WheelHouse reads the screen) and "
        "wh-overlay-slow-uia-stale-badges.11 (bounded queue, stale-command "
        "expiry, cancellation in the Input command loop). Today there is no "
        "dictation gate at all: a case-insensitive grep for "
        "'refuse.*dictat|dictat.*gate|gate.*dictat' over main.py and speech/ "
        "matched nothing on 2026-08-29, and the first half of this journey "
        "measures the consequence -- a word typed while the read is "
        "outstanding. Strict on purpose: whoever lands child .7 deletes this "
        "marker."
    ),
)
async def test_journey_5_a_destroyed_read_command_never_leaves_dictation_gated(
    journey_rig, tmp_path,
):
    """Journey 5: the read command is destroyed, and dictation must recover.

    WheelHouse starts reading the screen. The Input process runs ONE
    sequential command loop, so everything queued behind that read is stuck
    until it ends; a word typed late lands in whatever window is in front by
    then. Child .7's answer is to REFUSE dictation while the read is
    outstanding rather than queue it -- late dictation is worse than lost
    dictation.

    The read command is then destroyed: nothing ever answers it. The gate
    must not stay closed for that. This journey is therefore two assertions
    that must BOTH hold, and either one alone would be satisfied by a broken
    implementation: a gate that never closes passes the second, and a gate
    that never opens passes the first.
    """

    controller = journey_rig.controller
    processor = _real_speech_processor(journey_rig, tmp_path)

    # --- WheelHouse starts reading the screen ------------------------------
    controller._apply_overlay_event(
        OverlayEvent(OverlayEventKind.SHOW_NUMBERS),
        source="journey-5 show numbers",
    )
    assert controller.click_overlay_state.state is OverlayState.WALK_IN_FLIGHT
    session_id = controller.click_overlay_state.overlay_session_id
    generation = controller.click_overlay_state.paint_generation
    await _await_frame(journey_rig.command_ready_event)
    read_action, _params, _has, _rid, _trace = _read_command_frame(
        journey_rig.shm,
    )
    assert read_action == "start_overlay_walk"
    # Copied and cleared, so the Input side has taken the read command. It is
    # then DESTROYED: nothing will ever answer it.
    journey_rig.command_ready_event.clear()

    # ASSERTION 1: the user speaks while the read is outstanding, and nothing
    # is typed.
    during = await _dictate_and_collect_frames(
        journey_rig, processor, _A_DICTATED_WORD,
    )
    assert "intelligent_insert_text" not in during, (
        "a word was typed while WheelHouse was still reading the screen; the "
        "Input command loop is sequential, so it lands seconds late in "
        f"whatever window is in front by then (actions seen: {during!r})"
    )

    # --- the destroyed read expires ----------------------------------------
    # The build-failure feed production's own ``_feed`` closure produces on a
    # timeout, fed through the same entry point it uses.
    controller._apply_overlay_event(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=session_id,
            paint_generation=generation,
            snapshot_id=None,
            build_ok=False,
        ),
        source="journey-5 read expired",
    )
    assert controller.click_overlay_state.state is OverlayState.CLOSED

    # ASSERTION 2: the gate did not stick. The same word now types.
    after = await _dictate_and_collect_frames(
        journey_rig, processor, _A_DICTATED_WORD,
    )
    assert "intelligent_insert_text" in after, (
        "dictation is still refused after the read command was destroyed; "
        "the gate has no release, so the user's voice is gone until they "
        f"restart (actions seen: {after!r})"
    )


# ---------------------------------------------------------------------------
# Journey 7 -- a slow accessibility provider, and an 8-second read
# ---------------------------------------------------------------------------


def _write_slow_read_config(tmp_path: Path) -> Path:
    """A ``[click]`` block whose screen read may take 8 seconds.

    8000 ms is the measured case from the parent bug: on 2026-08-11 a read ran
    8285 ms, and on the same application a 7031 ms read returned the CORRECT
    new content. ``screen_read_timeout_ms`` is the screen read's own limit
    (wh-overlay-slow-uia-stale-badges.3), so 8000 is accepted here even though
    ``response_timeout_ms`` stays at its 3000 ms default.
    """

    slow = tmp_path / "slow-read-config.toml"
    slow.write_text(
        "[click]\nenabled = true\nscreen_read_timeout_ms = 8000\n",
        encoding="utf-8",
    )
    return slow


async def test_journey_7_a_slow_read_types_nothing_into_the_window_that_took_over(
    tmp_path,
):
    """Journey 7: the accessibility provider is slow and the read takes 8 s.

    Two things have to be true together, and the bead pairing is why they are
    one journey rather than two. Child .3 lets the read RUN for 8 seconds
    instead of throwing a correct answer away at 2500 ms; children .7 and .11
    are the containment that makes a longer read safe, because the Input
    command loop is sequential and a longer read means a longer window in
    which a dictated word can be typed late into a window that has since
    changed. Child .3's own note says it must not ship alone.

    The 8 seconds are not waited out here. A read that never answers is the
    same read from Logic's side for as long as the journey watches it, and
    the per-test cap is 30 s (pyproject.toml ``timeout``); what is asserted
    is that nothing is typed WHILE it is outstanding.
    """

    from ui.click_config import ClickConfig

    # PART 1 -- child .3: the read limit is its own, not the reply limit's.
    slow = ClickConfig.from_raw(
        {"enabled": True, "screen_read_timeout_ms": 8000}
    )
    assert slow.invalid_key is None, (
        "an 8-second screen read was rejected as an invalid [click] value "
        f"(invalid_key={slow.invalid_key!r}); the read limit is still "
        "derived from response_timeout_ms, so a read that finishes after "
        "2500 ms with the correct content is discarded"
    )
    assert slow.enabled
    assert slow.screen_read_timeout_ms == 8000

    # PART 2 -- children .7 and .11: nothing types while that read runs.
    data_dir = tmp_path / "journey-7-data"
    data_dir.mkdir()
    async with _real_logic_controller(
        data_dir, config_path=_write_slow_read_config(tmp_path),
    ) as rig:
        controller = rig.controller
        assert controller.click_config.screen_read_timeout_ms == 8000
        processor = _real_speech_processor(rig, tmp_path)

        # The limit the REQUEST carries, recorded as Logic hands it over.
        # Storing 8000 in the config is not the same as reading it at
        # dispatch: ``_overlay_dispatch_build`` in main.py chooses the awaited
        # window and passes it to ``app.send_request``, and this journey's
        # config file leaves response_timeout_ms at its 3000 ms default. So a
        # child .3 that only widened what ClickConfig ACCEPTS would store
        # 8000, expire the read at 3 s anyway, feed build_ok=False, and throw
        # away the correct 7031 ms answer the parent bug measured -- with
        # PART 1 above fully green. The dispatch must read
        # ``screen_read_timeout_ms`` for a start_overlay_walk build.
        sent: list[tuple[str, float | None]] = []
        real_send_request = rig.app.send_request

        async def _recording_send_request(
            action, params=None, timeout_s=None, on_late_response=None,
        ):
            sent.append((action, timeout_s))
            return await real_send_request(
                action,
                params=params,
                timeout_s=timeout_s,
                on_late_response=on_late_response,
            )

        rig.app.send_request = _recording_send_request

        controller._apply_overlay_event(
            OverlayEvent(OverlayEventKind.SHOW_NUMBERS),
            source="journey-7 slow read",
        )
        assert controller.click_overlay_state.state is (
            OverlayState.WALK_IN_FLIGHT
        )
        await _await_frame(rig.command_ready_event)
        read_action, _params, _has, _rid, _trace = _read_command_frame(rig.shm)
        assert read_action == "start_overlay_walk"
        rig.command_ready_event.clear()

        # The 8 seconds are not waited out here. The timeout VALUE on the
        # request is ONE of the two guards a 7-second answer has to clear;
        # the other is the overlay machine's own walk deadline, derived from
        # the same key by ``make_click_overlay_state_machine``. This journey
        # pins the value only (wh-overlay-slow-uia-stale-badges.3.1.3). The
        # reply itself is DELIVERED at a simulated 7.0 s, and proved to be
        # applied end to end -- BUILD_RESPONSE with build_ok True, the
        # snapshot cached, the paint sent -- across both guards, by this
        # test in tests/test_logic_overlay_integration.py:
        # test_a_read_that_answers_after_the_reply_limit_but_inside_the_read_limit_is_applied
        walk_timeouts = [t for a, t in sent if a == "start_overlay_walk"]
        assert walk_timeouts == [8.0], (
            "the screen read was dispatched with "
            f"{walk_timeouts!r} seconds, not the configured 8.0; the read "
            "limit is still the REPLY limit (response_timeout_ms, 3000 ms "
            "here), so a read that returns the correct content at 7031 ms is "
            "thrown away even though [click] screen_read_timeout_ms says 8000"
        )

        during = await _dictate_and_collect_frames(
            rig, processor, _A_DICTATED_WORD,
        )
        assert "intelligent_insert_text" not in during, (
            "a word was typed while a slow accessibility provider still had "
            "the Input command loop; with an 8-second read allowed, that "
            "word arrives up to 8 seconds late in whatever window is in "
            f"front by then (actions seen: {during!r})"
        )


# ---------------------------------------------------------------------------
# The mouse grid, end to end -- bead wh-grid-integration
# ---------------------------------------------------------------------------
#
# WHY THESE LIVE HERE. The grid's own test files split cleanly in two and
# nothing joined the halves. tests/test_grid_number_badge_click.py and
# tests/test_grid_closed_fallback_retraction.py build a real PatternCatalog
# and a real SpeechProcessor over a HAND-WRITTEN fake logic controller
# (_FakeLogicController at :109, _ClosedGridLc at :88). tests/
# test_grid_speech_routing.py and tests/test_grid_session_fence.py build a
# real GridOverlayStateMachine under a MagicMock(spec=LogicController)
# (:146 and :82) and pass the command string straight to
# handle_grid_command -- `grep -n "SpeechProcessor" tests/
# test_grid_speech_routing.py` returns no match, so no speech stack runs
# there at all. This file held the suite's only real LogicController
# (_real_logic_controller below) and, before this section, `grep -c -i
# "grid"` over it returned 0.
#
# So these four journeys are the first tests in which a spoken utterance
# reaches the grid through the shipped PatternCatalog, the shipped
# TextParser and ActionFunctions, a real SpeechProcessor and a real
# LogicController, with the paints read off a real multiprocessing queue
# and the pointer actions read back out of real shared memory.
#
# The one seam is the monitor topology: `_grid_monitor_context`
# (main.py:5110) is the blocking Win32 read, and its own docstring names it
# a seam. A journey that enumerated this machine's monitors would assert
# different rectangles on every desktop, so the topology is injected the
# way DisplayRig above injects one.


# A fixed two-monitor desktop in virtual-desktop physical pixels, the shape
# GridEvent carries. The second monitor is deliberately a different size and
# to the right of the first, so "grid next screen" cannot pass by painting
# the same rectangle twice.
def _grid_monitors():
    from services.wheelhouse.grid_overlay_state import GridRect

    return (GridRect(0, 0, 1920, 1080), GridRect(1920, 0, 2560, 1440))


def _grid_speech_processor(rig: JourneyRig):
    """A real SpeechProcessor whose whole command path is production's.

    ``SpeechHandler.__init__`` (speech/speech_handler.py:57) takes exactly
    the three things the rig already owns -- the real app, the real
    LogicController and the config service the rig built from the tracked
    ``config.toml.example`` -- and constructs the real ``PatternCatalog``
    (:84) and the real ``TextParser`` (:87) itself. That TextParser is why
    this helper exists instead of reusing ``_real_speech_processor`` above:
    that one passes ``text_parser=None``, which is right for a plain
    dictated word and useless for a command. `grep -n "self\\.text_parser"
    speech/speech_processor.py` shows the command path reaching it at
    :2463 and :2686.

    Hermeticity comes free from the rig: ``_real_logic_controller`` patches
    ``utils.system.get_user_data_dir`` to a tmp_path, and
    ``_resolve_user_patterns_file`` (speech_handler.py:88) resolves the
    writable user-patterns file under it, so this machine's editable
    overrides are never read.
    """

    from speech.speech_handler import SpeechHandler

    handler = SpeechHandler(
        rig.app, rig.controller, rig.controller.config_service,
    )
    assert handler.pattern_catalog.pattern_count > 0, (
        "the catalog loaded in degraded mode (pattern_count == 0), so every "
        "utterance below would dictate and prove nothing about the grid"
    )
    handler.initialize_speech_processor(asyncio.Queue())
    return handler.speech_processor


async def _feed_utterance(processor, words: list) -> None:
    """Speak one utterance: the words, then a real end-of-utterance marker.

    The separate marker event is load-bearing, not ceremony. Every shipped
    grid pattern carries ``whole_utterance_only = true``
    (speech/config/patterns.toml:2697-2805), so the router BUFFERS a
    matching buffer and waits for the utterance to end rather than firing at
    word speed (speech/router.py:520-527). Measured while building this
    section: with ``end_of_utterance=True`` on the last word and no marker,
    "show grid" still opened the grid but "three" never refined it. The
    marker's shape is the one tests/test_speech_processor_bare_number.py:147
    already uses.
    """

    from speech.word_event import WordEvent

    for index, word in enumerate(words):
        await processor.process_word_event(
            WordEvent(
                word=word,
                start_of_utterance=(index == 0),
                end_of_utterance=(index == len(words) - 1),
                utterance_id=1,
            )
        )
    await processor.process_word_event(
        WordEvent(
            word="",
            start_of_utterance=False,
            end_of_utterance=True,
            utterance_id=1,
            is_utterance_end_marker=True,
        )
    )


async def _say(
    rig: JourneyRig, processor, words: list, *, window_s: float = 5.0,
) -> list:
    """Speak one utterance and report every action that reached Input.

    Await processing while reading the real Input transport. A click gets
    a schema-valid success reply, so the utterance can finish instead of
    being cancelled while awaiting Input. Journey D supplies its own drag
    failure reply. The final request is a test-only FIFO barrier: seeing
    its frame proves that earlier fire-and-forget frames were collected.

    ``window_s`` is a fixture failure bound, never a successful observation
    window. A stalled utterance must report its cause here, rather than
    leaving the next journey assertion to blame a still-closed grid.
    """
    from services.wheelhouse.shared.mouse_action import MouseActionResponse

    actions: list = []
    barrier = "_grid_journey_input_barrier"

    async def speak_and_flush():
        await _feed_utterance(processor, words)
        # Logic dispatches pointer work in background tasks so speech stays
        # responsive. Include that work before placing the FIFO barrier;
        # otherwise a click can be enqueued behind the barrier and missed.
        await asyncio.gather(
            *getattr(rig.controller, "_pending_grid_mouse_actions", ()),
        )
        reply = await rig.app.send_request(barrier, {}, timeout_s=window_s)
        assert reply.get("status") == "ok", (
            f"grid journey fixture Input barrier failed: {reply!r}"
        )

    async def collect():
        while True:
            if rig.command_ready_event.is_set():
                frame = _read_command_frame(rig.shm)
                rig.command_ready_event.clear()
                action, params, _has, request_id, _trace = frame
                if action == barrier:
                    rig.app.response_queue.put({
                        "status": "ok", "action": action,
                        "request_id": request_id,
                    })
                    return
                actions.append(frame)
                if request_id is not None:
                    assert action == "click_point", (
                        "grid journey fixture has no reply for "
                        f"unexpected request {action!r}"
                    )
                    reply = MouseActionResponse(
                        status="ok", outcome="ok", reason=None,
                        trace_id=params["trace_id"],
                    ).to_dict()
                    reply.update(action=action, request_id=request_id)
                    rig.app.response_queue.put(reply)
            await asyncio.sleep(0.001)

    tasks = [asyncio.create_task(speak_and_flush()), asyncio.create_task(collect())]
    deadline = asyncio.timeout(window_s)
    try:
        async with deadline:
            await asyncio.gather(*tasks)
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        raise AssertionError(
            f"grid journey fixture timed out processing {' '.join(words)!r} "
            f"or delivering its Input frames within {window_s}s; "
            f"received {[frame[0] for frame in actions]!r}"
        ) from exc
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return actions


def _grid_gui_actions(rig: JourneyRig) -> list:
    """Drain the real state-to-GUI queue and name the actions on it.

    The grid's paints do not cross the Logic-to-Input boundary; they go to
    the GUI process on ``state_to_gui_queue`` as ``paint_grid`` /
    ``paint_grid_pin`` / ``clear_grid`` dicts, and a click notice arrives on
    the same queue as ``show_click_notice`` (main.py:9713). A real
    ``multiprocessing.Queue`` is used rather than a list double for the
    reason this file's own docstring gives: it proves the payload can be
    PICKLED, which the shipped spawn start method actually requires.
    """

    # Queue.put returns before its feeder writes to the pipe. A FIFO marker
    # makes collection depend on delivery, not an arbitrary settling sleep.
    barrier = ("_grid_journey_gui_barrier",)
    rig.state_to_gui_queue.put(barrier)
    deadline = time.monotonic() + 5.0
    seen: list = []
    while True:
        try:
            message = rig.state_to_gui_queue.get(
                timeout=max(0.0, deadline - time.monotonic()),
            )
        except (queue_mod.Empty, OSError, ValueError) as exc:
            raise AssertionError(
                "grid journey fixture could not deliver its GUI queue barrier"
            ) from exc
        if message == barrier:
            return seen
        seen.append(message)


def _grid_gui_names(rig: JourneyRig) -> list:
    return [
        msg.get("action")
        for msg in _grid_gui_actions(rig)
        if isinstance(msg, dict)
    ]


async def test_grid_say_waits_for_processing_after_collection_clock_expires(
    journey_rig, monkeypatch,
):
    """A descheduled collector must not silently cancel 'show grid'."""
    from threading import Event

    rig = journey_rig
    monitors = _grid_monitors()
    rig.controller._grid_monitor_context = lambda: (monitors, monitors[0])
    processor = _grid_speech_processor(rig)
    # Hold the real feeder until the drain offers a blocking read. A zero-
    # timeout poll cannot see a paint still held in the feeder's buffer.
    messages = rig.state_to_gui_queue
    assert messages._thread is None, "control the feeder before it starts"
    release = Event()
    send_bytes, get = messages._send_bytes, messages.get

    def delayed_send(data):
        assert release.wait(2.0), "GUI fixture feeder was never released"
        send_bytes(data)

    def read(*args, **kwargs):
        if kwargs.get("timeout", 0.0) > 0.0:
            release.set()
        return get(*args, **kwargs)

    monkeypatch.setattr(messages, "_send_bytes", delayed_send)
    monkeypatch.setattr(messages, "get", read)
    # One real 1.0 s host stall of the event-loop thread. _say's speech task
    # first runs after its deadline has started, so the stall lands inside
    # that window with every word, the command and the Input barrier to go.
    process_word_event = processor.process_word_event
    stalled: list = []

    async def stall_first_word(event):
        if not stalled:
            stalled.append(event.word)
            time.sleep(1.0)
        return await process_word_event(event)

    processor.process_word_event = stall_first_word
    try:
        frames = await _say(rig, processor, ["show", "grid"])

        assert stalled == ["show"], "the host stall never ran"
        assert rig.controller.grid_overlay_state.is_open, (
            "the fixture returned before the accepted show-grid utterance ran"
        )
        assert "end_utterance" in [frame[0] for frame in frames], (
            "the fixture returned before the final fire-and-forget frame arrived"
        )
        assert "paint_grid" in _grid_gui_names(rig)
    finally:
        release.set()


async def test_grid_say_propagates_speech_failure(journey_rig):
    class BrokenProcessor:
        async def process_word_event(self, event):
            raise RuntimeError("speech fixture probe failed")

    with pytest.raises(RuntimeError, match="speech fixture probe failed"):
        await _say(journey_rig, BrokenProcessor(), ["show", "grid"])


async def test_grid_say_attributes_timeout_and_cleans_up_speech(journey_rig):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockedProcessor:
        async def process_word_event(self, event):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    with pytest.raises(AssertionError, match="grid journey fixture.*show grid"):
        await _say(
            journey_rig, BlockedProcessor(), ["show", "grid"], window_s=0.01,
        )
    assert entered.is_set()
    assert cancelled.is_set(), "the timed-out speech task was left running"


def test_grid_gui_collection_waits_for_feeder_delivery(monkeypatch):
    """A temporarily empty pipe does not mean the GUI had no paint."""
    from threading import Event
    from types import SimpleNamespace

    release = Event()
    messages = MpQueue()
    send_bytes = messages._send_bytes
    get = messages.get

    def delayed_send(data):
        assert release.wait(2.0), "GUI fixture feeder was never released"
        send_bytes(data)

    def read(*args, **kwargs):
        block = args[0] if args else kwargs.get("block", True)
        if not block:
            raise queue_mod.Empty
        release.set()
        return get(*args, **kwargs)

    monkeypatch.setattr(messages, "_send_bytes", delayed_send)
    monkeypatch.setattr(messages, "get", read)
    messages.put({"action": "paint_grid"})
    try:
        assert _grid_gui_actions(SimpleNamespace(state_to_gui_queue=messages)) == [
            {"action": "paint_grid"},
        ]
    finally:
        release.set()
        messages.close()
        messages.join_thread()


# ---------------------------------------------------------------------------
# Grid journey A -- show the grid, refine it by number, then click
# ---------------------------------------------------------------------------


# The strict xfail this test carried is gone, and its removal is the point.
# It named wh-grid-integration criterion 2: a spoken "click" reached
# dictation instead of grid_click_command, because
# PatternCatalog._extract_first_words returned only ['tap'] for the shipped
# pattern ^((?:click|tap)[.!?]?)$. wh-grid-click-nested-group fixed both
# halves -- the extractor now strips a non-capturing prefix one group deep,
# and patterns.toml carries the unnested ^((click|tap)[.!?]?)$ -- so the
# marker was strict for exactly this day and deleting it is the signal it
# was written to give.
async def test_grid_journey_a_show_refine_then_click(journey_rig):
    """Grid journey A: "show grid", "three", "click".

    The whole point of the sequence is that each utterance depends on the
    one before it. "three" means nothing until the grid is open, and
    "click" means nothing until a cell has been chosen -- so a test that
    starts from a fixture-set open state, as every existing grid test does,
    cannot show that speaking the words in order actually gets there.
    """

    rig = journey_rig
    monitors = _grid_monitors()
    rig.controller._grid_monitor_context = lambda: (monitors, monitors[0])
    machine = rig.controller.grid_overlay_state
    processor = _grid_speech_processor(rig)

    # --- "show grid" -------------------------------------------------------
    await _say(rig, processor, ["show", "grid"])
    assert machine.is_open, (
        "'show grid' did not open the grid; nothing after this can mean "
        "anything"
    )
    assert machine.rect == monitors[0], (
        f"the grid opened on {machine.rect!r}, not on the focused monitor "
        f"{monitors[0]!r}"
    )
    assert "paint_grid" in _grid_gui_names(rig), (
        "the grid opened in Logic but no paint_grid reached the GUI queue, "
        "so nothing would be on screen for the user to refine"
    )

    # --- "three" refines to the top-right ninth ----------------------------
    # Cell 3 of a telephone keypad is the top-right ninth of a 1920x1080
    # monitor: left 1280, top 0, 640 wide, 360 tall, centred at (1600, 180).
    await _say(rig, processor, ["three"])
    assert machine.rect is not None and machine.rect.as_tuple() == (
        1280, 0, 640, 360,
    ), (
        f"'three' left the grid at {machine.rect!r}; a bare number spoken "
        "with the grid open must narrow it to that cell, not dictate"
    )
    assert machine.current_center is not None
    refined_center = machine.current_center
    assert (refined_center.x, refined_center.y) == (1600, 180)
    assert "paint_grid" in _grid_gui_names(rig), (
        "the grid narrowed in Logic but no repaint reached the GUI queue"
    )

    # --- "click" acts at that cell's centre ---------------------------------
    # This is the assertion the strict xfail marker was about. Before
    # wh-grid-click-nested-group the frame that crossed the boundary here was
    # ('intelligent_insert_text', {'insertion_string': 'click'}, ...), because
    # the first-word index had lost "click" and the router sent it to
    # dictation. Both words route to grid_click_command now.
    frames = await _say(rig, processor, ["click"])
    by_action = {action: params for action, params, _h, _r, _t in frames}
    assert "intelligent_insert_text" not in by_action, (
        "the grid was open on a refined cell and 'click' was TYPED instead "
        f"of clicking (frames seen: {sorted(by_action)!r})"
    )
    assert "click_point" in by_action, (
        "'click' with the grid open sent no click to Input (frames seen: "
        f"{sorted(by_action)!r})"
    )
    params = by_action["click_point"]
    assert (params.get("x"), params.get("y")) == (1600, 180), (
        f"the click landed at {(params.get('x'), params.get('y'))!r}, not at "
        "the refined cell's centre (1600, 180)"
    )
    assert not machine.is_open, (
        "the grid stayed open after a click; the action closes the session"
    )
    assert "show_click_notice" not in _grid_gui_names(rig), (
        "the successful Input click reply became a failure notice"
    )


# ---------------------------------------------------------------------------
# Grid journey B -- "grid next screen" across a two-monitor desktop
# ---------------------------------------------------------------------------


async def test_grid_journey_b_next_screen_walks_the_desktop(journey_rig):
    """Grid journey B: the closed no-op, then two monitors and the wrap.

    The closed case is asserted FIRST and on purpose. main.py:5289 makes
    "grid next screen" with the grid closed a silent no-op that is still
    CONSUMED, so the failure it guards against is not a crash -- it is the
    words "grid next screen" being typed into the user's document.
    """

    rig = journey_rig
    monitors = _grid_monitors()
    rig.controller._grid_monitor_context = lambda: (monitors, monitors[0])
    machine = rig.controller.grid_overlay_state
    processor = _grid_speech_processor(rig)

    # --- with the grid closed: consumed, silent, and never typed -----------
    frames = await _say(rig, processor, ["grid", "next", "screen"])
    typed = [a for a, _p, _h, _r, _t in frames if a == "intelligent_insert_text"]
    assert typed == [], (
        "'grid next screen' with the grid closed was typed as text; "
        "main.py:5289 makes it a consumed no-op"
    )
    assert not machine.is_open
    assert _grid_gui_names(rig) == [], (
        "a closed grid painted something for 'grid next screen'"
    )

    # --- open on the focused monitor ---------------------------------------
    await _say(rig, processor, ["show", "grid"])
    assert machine.monitor == monitors[0]
    assert "paint_grid" in _grid_gui_names(rig)

    # --- and step to the second one ----------------------------------------
    await _say(rig, processor, ["grid", "next", "screen"])
    assert machine.monitor == monitors[1], (
        f"'grid next screen' left the grid on {machine.monitor!r}; it must "
        f"move to {monitors[1]!r}"
    )
    assert machine.rect == monitors[1], (
        "the grid moved monitors but did not restart at that monitor's full "
        f"rectangle (rect={machine.rect!r})"
    )
    assert "paint_grid" in _grid_gui_names(rig), (
        "the grid moved in Logic but the GUI was never told to repaint, so "
        "the user would still see it on the old screen"
    )

    # --- and wrap around ----------------------------------------------------
    await _say(rig, processor, ["grid", "next", "screen"])
    assert machine.monitor == monitors[0], (
        "'grid next screen' on the last monitor must wrap to the first, not "
        f"stop (monitor={machine.monitor!r})"
    )
    assert "paint_grid" in _grid_gui_names(rig)


# ---------------------------------------------------------------------------
# Grid journey C -- "dismiss grid" takes it off the screen
# ---------------------------------------------------------------------------


async def test_grid_journey_c_dismiss_clears_the_screen(journey_rig):
    """Grid journey C: "show grid" then "dismiss grid".

    Two things must BOTH happen, and either alone would be satisfied by a
    broken implementation: Logic must forget the session, and the GUI must
    be told to erase what it drew. A dismiss that only did the first would
    leave a grid painted over the user's screen with nothing listening to
    it.
    """

    rig = journey_rig
    monitors = _grid_monitors()
    rig.controller._grid_monitor_context = lambda: (monitors, monitors[0])
    machine = rig.controller.grid_overlay_state
    processor = _grid_speech_processor(rig)

    await _say(rig, processor, ["show", "grid"])
    assert machine.is_open
    assert "paint_grid" in _grid_gui_names(rig)

    frames = await _say(rig, processor, ["dismiss", "grid"])
    typed = [a for a, _p, _h, _r, _t in frames if a == "intelligent_insert_text"]
    assert typed == [], (
        "'dismiss grid' was typed as text instead of dismissing the grid"
    )
    assert not machine.is_open, "'dismiss grid' left the grid open in Logic"
    assert machine.rect is None and machine.mark is None, (
        f"dismiss left state behind (rect={machine.rect!r}, "
        f"mark={machine.mark!r}); a stale mark can cause a surprise drag in "
        "a later session"
    )
    assert "clear_grid" in _grid_gui_names(rig), (
        "Logic closed the grid but never told the GUI to erase it, so the "
        "painted grid would stay on the user's screen"
    )


# ---------------------------------------------------------------------------
# Grid journey D -- mark, then drag, and the button that may still be held
# ---------------------------------------------------------------------------


async def _answer_grid_action(
    rig: JourneyRig, *, expect_action: str, reason, window_s: float = 2.0,
):
    """Read frames until ``expect_action``, then answer it as Input would.

    Skips the fire-and-forget frames an utterance also produces (the
    ``end_utterance`` marker carries request_id None, so it can never be
    answered). The reply is built with the PRODUCTION schema
    ``MouseActionResponse`` (shared/mouse_action.py:72) rather than a
    hand-written dict, so this journey cannot craft a payload the real
    handler could never emit -- Logic parses it with exactly that
    classmethod (main.py:5843). It goes onto the real
    ``app.response_queue`` and is matched to its Future by the real
    demuxer, the same path ``_answer_input_request`` above documents.

    ``reason=None`` answers success; a string answers execution_failed with
    that reason.
    """

    from services.wheelhouse.shared.mouse_action import MouseActionResponse

    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if rig.command_ready_event.is_set():
            action, params, _has, request_id, _trace = _read_command_frame(
                rig.shm,
            )
            rig.command_ready_event.clear()
            if action != expect_action:
                continue
            assert request_id is not None, (
                f"{action} went out fire-and-forget, so nothing can answer it"
            )
            reply = MouseActionResponse(
                status="ok" if reason is None else "error",
                outcome="ok" if reason is None else "execution_failed",
                reason=reason,
                trace_id=params["trace_id"],
            ).to_dict()
            reply["request_id"] = request_id
            reply["action"] = action
            rig.app.response_queue.put(reply)
            return params
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"no {expect_action!r} command reached Input within {window_s}s"
    )


async def test_grid_journey_d_mark_then_drag_reports_a_stuck_button(
    journey_rig,
):
    """Grid journey D: "show grid", "one", "mark", "nine", "drag".

    A drag is the only grid action that presses the button in one cell and
    releases it in another, so it is the only one whose failure can leave
    the mouse button HELD -- and a held button makes the desktop unusable
    for someone who drives it by voice. The primitive's release guarantee
    is already pinned three layers down
    (tests/test_win_mouse_primitives.py:604-701 against a FakeUser32) and
    the Input handler's propagation one layer down
    (tests/test_ui/test_mouse_pointer_handlers.py:255). What no test showed
    is that a user who SPEAKS the two words is told about it: this journey
    fails the drag the way Input reports a refused release and asserts the
    distinct notice reaches the GUI.

    The mark must also survive the refinement between the two words. That
    is the whole reason the grid has a separate "mark": the anchor is set
    in one cell and the destination chosen afterwards.
    """

    rig = journey_rig
    monitors = _grid_monitors()
    rig.controller._grid_monitor_context = lambda: (monitors, monitors[0])
    machine = rig.controller.grid_overlay_state
    processor = _grid_speech_processor(rig)

    await _say(rig, processor, ["show", "grid"])
    assert machine.is_open
    _grid_gui_names(rig)

    # Cell 1 is the top-left ninth of 1920x1080, centred at (320, 180).
    await _say(rig, processor, ["one"])
    assert machine.current_center is not None
    assert (machine.current_center.x, machine.current_center.y) == (320, 180)

    await _say(rig, processor, ["mark"])
    assert machine.mark is not None, "'mark' set no drag anchor"
    assert (machine.mark.x, machine.mark.y) == (320, 180)
    assert "paint_grid_pin" in _grid_gui_names(rig), (
        "the anchor was set in Logic but no pin reached the GUI, so the "
        "user cannot see where the drag will start"
    )

    # Cell 9 is the bottom-right ninth, centred at (1600, 900). The mark
    # must survive this.
    await _say(rig, processor, ["nine"])
    assert (machine.mark.x, machine.mark.y) == (320, 180), (
        "refining the grid after 'mark' moved or lost the anchor"
    )
    assert machine.current_center is not None
    assert (machine.current_center.x, machine.current_center.y) == (1600, 900)

    # --- "drag", failed at the release ------------------------------------
    feed = asyncio.ensure_future(_feed_utterance(processor, ["drag"]))
    try:
        params = await _answer_grid_action(
            rig, expect_action="perform_drag", reason="release_failed",
        )
        assert (
            params["start_x"], params["start_y"],
            params["end_x"], params["end_y"],
        ) == (320, 180, 1600, 900), (
            f"the drag Input was asked to perform ran {params!r}, not from "
            "the marked cell's centre to the refined cell's centre"
        )
        # The notice is put on the GUI queue from the awaiting task, so give
        # it the loop back until it appears.
        deadline = time.monotonic() + 2.0
        notices: list = []
        while time.monotonic() < deadline and not notices:
            notices = [
                msg for msg in _grid_gui_actions(rig)
                if isinstance(msg, dict)
                and msg.get("action") == "show_click_notice"
            ]
            if not notices:
                await asyncio.sleep(0.01)
    finally:
        if not feed.done():
            feed.cancel()
        await asyncio.gather(feed, return_exceptions=True)

    assert notices, (
        "the drag was refused at the button release and the user was told "
        "nothing; the mouse button may still be held"
    )
    reasons = [n.get("reason") for n in notices]
    assert "grid_button_release_failed" in reasons, (
        "a refused release must get its OWN notice -- the one wording that "
        "says the button may still be down -- not the generic drag failure "
        f"(reasons seen: {reasons!r})"
    )
    assert not machine.is_open, (
        "the grid stayed open after the drag; the action closes the session "
        "whether or not Input could carry it out"
    )
