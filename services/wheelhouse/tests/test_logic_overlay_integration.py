"""Logic-side effect-performing overlay integration tests (wh-n29v.95).

This slice replaces the three Logic-process STUB SEAMS that previously only
LOGGED -- ``_dispatch_snapshot_item_click``, ``_hold_click_n``, and
``_perform_overlay_effects`` -- with the real effect-performing integration so a
numbered-overlay item click executes end to end:

  * Part 1: a real ``click_snapshot_item`` send to Input for BOTH the voice
    ``click N`` path and the GUI-consumer FOUND branch, consuming the
    ``ClickElementResponse`` and forwarding the click-notice on any non-ok
    outcome (no notice on ok).
  * Part 2: the async build-dispatch performer (start_overlay_walk vs
    show_numbered_overlay by BuildReason) + per-state asyncio timeout timers, so
    a standalone 'show numbers' reaches PAINTED. Effect dispatch is serialized
    under an asyncio.Lock so a not-yet-completed clear from one ack is not
    reordered against a paint/clear from a later ack (wh-n29v.70.2).
  * Part 3: the 200ms hold-or-drop timer + state re-read for a 'click N' that
    arrives during a transition; never a silent drop.
  * Part 4: the build awaiters populate the summary cache, and the
    actively-painted snapshot is kept alive past the 30s TTL.
  * Part 5: ``_overlay_tracked_identity`` is assigned at the pin point and
    cleared on entry to closed.

The LogicController is built via ``object.__new__`` to skip the heavy
``__init__`` (the wh-n29v test precedent in
test_logic_overlay_state_changed_handler.py), injecting only the attributes
each path touches; its overlay comes from the public constructor. ``app.send_request`` and the
GUI state queue are fakes; effects and IPC are asserted as DATA.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import logging
import types
from contextlib import ExitStack, asynccontextmanager
from typing import Any, Optional, cast
from unittest.mock import MagicMock, patch

import pytest

from tests.overlay_boundary_harness import (
    FakeDesktop,
    FakeFocusHooks,
    OffsetClock,
    Window,
    paint_ack,
    settle,
)

from services.wheelhouse.click_overlay_state import (
    ClickOverlayStateMachine,
    EffectKind,
    OverlayEvent,
    OverlayEventKind,
    OverlayState,
)
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from services.wheelhouse.main import LogicController
from services.wheelhouse.numbered_overlay.controller import (
    NumberedOverlayController,
)
from services.wheelhouse.numbered_overlay.helpers import _OVERLAY_READ_SENTENCE_WAIT_MAX_S
from services.wheelhouse.shared.click_element import ClickElementResponse
from services.wheelhouse.shared.show_numbered_overlay import (
    ShowNumberedOverlayResponse,
)
from services.wheelhouse.shared.start_overlay_walk import (
    StartOverlayWalkResponse,
)
from services.wheelhouse.ui.element_types import (
    WalkSnapshotSummary,
    WalkSnapshotSummaryItem,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(snapshot_id: str, *display_numbers: int) -> WalkSnapshotSummary:
    items = [
        WalkSnapshotSummaryItem(
            item_id=f"{snapshot_id}-item-{n}",
            display_number=n,
            name=f"control {n}",
            role="Button",
            bounds=(0, 0, 10, 10),
            monitor_id=0,
        )
        for n in display_numbers
    ]
    return WalkSnapshotSummary(
        snapshot_id=snapshot_id, items=items, created_at_monotonic=1.0,
    )


class _FakeQueue:
    """A minimal state_to_gui_queue capturing put_nowait payloads."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_nowait(self, item: dict) -> None:
        self.items.append(item)


def _controller(
    *,
    enabled: bool = True,
    overlay_effective: bool = True,
    cache: Optional[ClickSnapshotSummaryCache] = None,
    machine: Optional[ClickOverlayStateMachine] = None,
    raw: Optional[dict] = None,
    clock=None,
):
    """Build a bare LogicController with only the integration attributes.

    Skips the heavy ``__init__`` via ``object.__new__`` and injects the few
    attributes the effect performer / awaiters / hold timer touch. The
    overlay itself is built through its public constructor, which reads the
    [click] config: a real config is in place while it runs (``raw``, or the
    defaults), and the MagicMock config replaces it afterwards. With ``raw``
    and no ``machine``, the machine is the one the constructor built from
    ``raw``. ``clock`` is the clock given to the constructor.
    """

    from services.wheelhouse.ui.click_config import ClickConfig

    controller = object.__new__(LogicController)
    controller.grid_overlay_state = None
    controller.click_config = ClickConfig.from_raw(dict(raw or {}))
    controller.overlay = NumberedOverlayController(
        controller, clock=clock if clock is not None else time.monotonic,
    )
    built_machine = controller.overlay.click_overlay_state
    controller.click_config = MagicMock()
    controller.click_config.enabled = enabled
    controller.click_config.overlay_enabled_effective = overlay_effective
    controller.click_config.overlay_invalid_key = None
    controller.click_config.response_timeout_ms = 3000
    controller.click_config.screen_read_timeout_ms = 10000
    # NB: use ``is None`` checks, NOT ``x or default`` -- an empty
    # ClickSnapshotSummaryCache has __len__ == 0 and is therefore FALSY, so
    # ``cache or ClickSnapshotSummaryCache()`` would silently DISCARD a freshly
    # constructed (empty) injected cache and substitute a different instance.
    if machine is not None:
        controller.overlay.click_overlay_state = machine
    elif raw is None:
        controller.overlay.click_overlay_state = ClickOverlayStateMachine()
    else:
        controller.overlay.click_overlay_state = built_machine
    controller.overlay.click_snapshot_summary_cache = (
        cache if cache is not None else ClickSnapshotSummaryCache()
    )
    # The timers, the effect lock, the identities and the clear and
    # restore-paint audits all start as the public constructor sets them.
    # Fake GUI queue.
    controller.state_manager = MagicMock()
    controller.state_manager.state_to_gui_queue = _FakeQueue()
    # wh-full-refactor.6.2: the grid methods live in MouseGridController,
    # which reaches this controller through its host back-reference.
    from services.wheelhouse.mouse_grid.controller import MouseGridController

    controller.mouse_grid = MouseGridController(controller)
    return controller


async def _settle(controller, *, turns: int = 12) -> None:
    """Pump the loop until overlay background tasks settle (test harness).

    The build-response feed is now deferred via ``loop.call_soon`` (wh-n29v.96.4),
    and that deferred transition schedules a FRESH OverlayEffects task (PIN/PAINT)
    that is not in any earlier ``background_tasks`` snapshot. A single
    ``await asyncio.gather(*background_tasks)`` therefore misses the follow-on
    work. This yields repeatedly, gathering newly-spawned tasks each turn, until
    no pending overlay task remains (bounded so a bug cannot hang the test).
    """

    for _ in range(turns):
        await asyncio.sleep(0)
        pending = [t for t in controller.background_tasks if not t.done()]
        if not pending:
            # Yield once more in case a call_soon callback is still queued.
            await asyncio.sleep(0)
            if all(t.done() for t in controller.background_tasks):
                break
        else:
            await asyncio.gather(*pending, return_exceptions=True)


def _wire_app(controller, responder):
    """Attach a fake app whose send_request defers to ``responder``.

    ``responder(action, params)`` returns the wire dict (a *.to_dict()).
    Captures every call on controller._sent (list of (action, params)).
    """

    sent: list[tuple[str, dict]] = []

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        sent.append((action, dict(params or {})))
        return responder(action, params or {})

    controller.app = MagicMock()
    controller.app.send_request = _send_request
    controller._sent = sent  # type: ignore[attr-defined]
    return sent


def _set_trace_noop(monkeypatch=None):
    pass


@pytest.fixture(autouse=True)
def _no_foreground_window(monkeypatch):
    """The real overlay's first-use hint reads the foreground window.

    With no foreground window the hint returns before it reads or writes
    its record file, so these tests never show a hint and never touch
    services/wheelhouse/data/. A test that installs a FakeDesktop patches
    the same Win32 call again inside its own scope.
    """
    import win32gui

    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 0)


# ---------------------------------------------------------------------------
# The public rig (wh-full-refactor.3.1): a bare controller reached only
# through public members and Win32 / Input / GUI seams.
# ---------------------------------------------------------------------------

# The window the numbers are painted over in the rig, unless a test names
# another one.
_RIG_WINDOW = Window(
    hwnd=0x4A01, pid=4101, process_name="rigapp.exe",
    class_name="RigMainWindow", create_time_s=1_700_000_000.0,
)

# The overlay outputs at the Logic boundary: requests to Input and messages
# to the GUI. The walking cue and the lease renew are left out.
_RIG_INPUT_ACTIONS = frozenset({
    "start_overlay_walk", "show_numbered_overlay", "pin_snapshot",
    "unpin_snapshot", "click_snapshot_item", "click_element",
})
_RIG_GUI_ACTIONS = frozenset({
    "paint_overlay", "clear_overlay", "show_click_notice",
})

# Win32 menu pop-up end (winuser.h EVENT_SYSTEM_MENUPOPUPEND).
_MENUPOPUPEND = 0x0007


class _LogQueue(_FakeQueue):
    """The state_to_gui_queue fake that also writes each message to the log."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self._log = log

    def put_nowait(self, item: dict) -> None:
        super().put_nowait(item)
        action = item.get("action") if isinstance(item, dict) else None
        self._log.append(("gui", action, item))


def _window_fields(window: Optional[Window]) -> dict:
    """The four identity keys a walk reply carries for the window it read."""

    if window is None:
        return {}
    return {
        "foreground_window": window.hwnd,
        "foreground_pid": window.pid,
        "foreground_process_name": window.process_name,
        "foreground_window_creation_time": window.creation_time_ms,
    }


class _Rig:
    """One bare controller, driven only through its public members.

    Inputs: the voice commands (``handle_overlay_command``,
    ``forward_click_element``), the GUI paint ack
    (``handle_overlay_state_changed``), the Input replies (the ``walk`` and
    ``click`` answerers below), the Win32 hook callbacks (``hooks``), the
    foreground window (``desktop``) and time (``clock``). Output: ``log``,
    every request to Input and every GUI message, in the order Logic
    produced them, as ``(kind, action, payload)``.
    """

    def __init__(self, controller, desktop: FakeDesktop, clock: OffsetClock):
        self.controller = controller
        self.overlay = controller.overlay
        self.machine = controller.overlay.click_overlay_state
        self.desktop = desktop
        self.clock = clock
        self.log: list = []
        self.walk_snapshots: list = []
        # The badge numbers an answered walk reports.
        self.walk_numbers: tuple = (1, 2, 3)
        self.held: list = []
        self.hold_walks = False
        # Per-action answerers: ``answers[action](params, on_late_response)``
        # returns the reply (or an awaitable of it), or raises.
        self.answers: dict = {}
        # Click sends whose answer has not returned yet.
        self.click_sends_open = 0

    @property
    def hooks(self) -> FakeFocusHooks:
        return FakeFocusHooks.instances[-1]

    # -- the Input side ------------------------------------------------------
    def _foreground_window(self) -> Optional[Window]:
        return self.desktop.windows.get(self.desktop.foreground)

    def walk_reply(self, params: dict, snapshot_id: str, numbers=(1, 2, 3),
                   read_from: Optional[Window] = None) -> dict:
        payload = _walk_response(
            snapshot_id, params.get("overlay_session_id"),
            params.get("paint_generation"), *numbers,
        )
        window = read_from if read_from is not None else self._foreground_window()
        payload.update(_window_fields(window))
        return payload

    async def _walk(self, params: dict) -> dict:
        if self.hold_walks:
            future = asyncio.get_running_loop().create_future()
            self.held.append((params, future))
            return await future
        snapshot_id = (
            self.walk_snapshots.pop(0) if self.walk_snapshots
            else f"walk-{params.get('paint_generation')}"
        )
        return self.walk_reply(params, snapshot_id, self.walk_numbers)

    def release(self, index: int, payload: dict) -> None:
        """Answer the held walk request number ``index``."""

        future = self.held[index][1]
        if not future.done():
            future.set_result(payload)

    async def _send_request(self, action, params=None, timeout_s=None,
                            on_late_response=None):
        params = dict(params or {})
        self.log.append(("input", action, params))
        if action == "click_snapshot_item":
            self.click_sends_open += 1
            try:
                return await self._answer(action, params, on_late_response)
            finally:
                self.click_sends_open -= 1
        return await self._answer(action, params, on_late_response)

    async def _answer(self, action, params, on_late_response):
        if action in self.answers:
            result: Any = self.answers[action](params, on_late_response)
        elif action == "start_overlay_walk":
            result = self._walk(params)
        elif action == "click_snapshot_item":
            result = _click_response("ok", trace_id=params.get("trace_id", "tr"))
        else:
            result = _pin_response(action, params)
        if inspect.isawaitable(result):
            result = await result
        return result

    # -- reading the log -----------------------------------------------------
    def mark(self) -> int:
        return len(self.log)

    def outputs(self, mark: int = 0) -> list:
        """``(kind, action)`` of each overlay output from ``mark`` on."""

        return [
            (kind, action) for kind, action, _payload in self.log[mark:]
            if (kind == "input" and action in _RIG_INPUT_ACTIONS)
            or (kind == "gui" and action in _RIG_GUI_ACTIONS)
        ]

    def payloads(self, action: str, mark: int = 0) -> list:
        return [
            payload for _kind, logged, payload in self.log[mark:]
            if logged == action
        ]

    def snapshot(self):
        """The machine's public fields now, for an assertion made later."""

        m = self.machine
        return types.SimpleNamespace(
            state=m.state, overlay_session_id=m.overlay_session_id,
            paint_generation=m.paint_generation,
            pinned_snapshot_id=m.pinned_snapshot_id,
            keep_numbers=m.keep_numbers,
        )

    # -- the voice and GUI inputs --------------------------------------------
    async def show(self, trace_id: str = "tr-show") -> None:
        await self.controller.handle_overlay_command("show", trace_id)
        await settle()

    async def ack(self, paint: dict, state: str = "painted") -> None:
        await self.overlay.handle_overlay_state_changed(paint_ack(
            state, paint["overlay_session_id"], paint["paint_generation"],
            paint.get("snapshot_id"),
        ))
        await settle()

    async def paint(self, snapshot_id: str, numbers=(1, 2, 3)) -> tuple:
        """'show numbers', the walk answered, the paint acknowledged."""

        self.walk_snapshots.append(snapshot_id)
        self.walk_numbers = tuple(numbers)
        mark = self.mark()
        await self.show()
        paints = [
            p for p in self.payloads("paint_overlay", mark)
            if p.get("snapshot_id") == snapshot_id
        ]
        assert paints, f"no paint_overlay for {snapshot_id}: {self.outputs(mark)}"
        await self.ack(paints[-1])
        from services.wheelhouse.click_overlay_state import OverlayState

        assert self.machine.state is OverlayState.PAINTED
        return (paints[-1]["overlay_session_id"], paints[-1]["paint_generation"])

    async def say_click(self, number: int, trace_id: str = "tr-ns") -> None:
        """A spoken "click N", through ``forward_click_element``."""

        from services.wheelhouse.ui.element_types import ElementQuery

        await self.controller.forward_click_element(
            ElementQuery(str(number), None, None, None, f"click {number}"),
            trace_id,
        )
        await settle()
        # An answerer that runs an interleave inside the send returns later
        # than one settle; wait (bounded) until every click send returned.
        deadline = time.perf_counter() + 5.0
        while self.click_sends_open and time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
        assert not self.click_sends_open, "a click send never returned"
        await settle()

    async def menu_close(self) -> None:
        """A menu closes over the numbered window (the Win32 hook callback)."""

        self.hooks.menu_popup(_MENUPOPUPEND)
        await settle()


@asynccontextmanager
async def _rig(raw: Optional[dict] = None, *, windows=(_RIG_WINDOW,),
               offset_clock: bool = False):
    """Start a ``_Rig`` with ``raw`` as the [click] config.

    The Win32 seams are the boundary harness's: ``FakeDesktop`` answers the
    foreground window, and ``FakeFocusHooks`` stands in for the hook thread
    that ``start_overlay_focus_hooks`` starts. ``offset_clock`` moves the
    loop clock and the overlay clock together (``rig.clock.advance``).
    """

    import services.wheelhouse.numbered_overlay.controller as overlay_module

    loop = asyncio.get_running_loop()
    FakeFocusHooks.instances = []
    desktop = FakeDesktop(windows)
    clock = OffsetClock()
    from services.wheelhouse.ui.click_config import ClickConfig

    settings = {
        "enabled": True, "enable_screen_reader_flag": False,
        "overlay_focus_debounce_ms": 0,
    }
    settings.update(raw or {})
    with ExitStack() as stack:
        desktop.install(stack)
        if offset_clock:
            clock.install(loop, stack)
        stack.enter_context(patch.object(
            overlay_module, "OverlayFocusHookManager", FakeFocusHooks,
        ))
        controller = _controller(raw=settings, clock=clock.monotonic)
        # The rig keeps the real [click] config, not the MagicMock one.
        controller.click_config = ClickConfig.from_raw(dict(settings))
        controller.loop = loop
        controller.background_tasks = []
        controller._click_disabled_notice_shown = False
        rig = _Rig(controller, desktop, clock)
        controller.app = MagicMock()
        controller.app.send_request = rig._send_request
        controller.state_manager.state_to_gui_queue = _LogQueue(rig.log)
        controller.overlay.start_overlay_focus_hooks()
        try:
            yield rig
        finally:
            for _params, future in rig.held:
                if not future.done():
                    future.cancel()
            controller.overlay.shutdown()
            pending = [t for t in controller.background_tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# PART 1: real click_snapshot_item send (voice path + GUI-consumer FOUND).
# ---------------------------------------------------------------------------


def _click_response(outcome: str, *, reason=None, matched_name=None,
                    trace_id="trace") -> dict:
    return ClickElementResponse(
        status="ok" if outcome == "ok" else "error",
        outcome=outcome,
        reason=reason,
        matched_names=(),
        snapshot_id=None,
        snapshot_summary=None,
        matched_name=matched_name,
        trace_id=trace_id,
    ).to_dict()


# ---------------------------------------------------------------------------
# PART 2: the async build-dispatch performer + timers (RELEASE GATE).
# ---------------------------------------------------------------------------


def _walk_response(snapshot_id, sid, gen, *display_numbers) -> dict:
    return StartOverlayWalkResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, *display_numbers),
        trace_id="tr",
        overlay_session_id=sid,
        paint_generation=gen,
    ).to_dict()


def _show_response(snapshot_id, sid, gen, *display_numbers) -> dict:
    return ShowNumberedOverlayResponse(
        status="ok",
        outcome="ok",
        reason=None,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, *display_numbers),
        trace_id="tr",
        overlay_session_id=sid,
        paint_generation=gen,
    ).to_dict()


# ---------------------------------------------------------------------------
# Trigger B (wh-overlay-snapshot-keepalive): per-snapshot window identity +
# the refresh-window-mismatch helper that feeds route_click_n. A focus-change
# refresh leaves the still-visible snapshot pinned to a window that is no longer
# foreground; routing must HOLD a 'click N' rather than dispatch a click Input
# would reject on a foreground-identity mismatch (snapshot_expired).
# ---------------------------------------------------------------------------


def _replay_through_gui_gate(effects) -> tuple[Optional[tuple[int, int]], list]:
    """Replay paint/clear effects in order through the REAL GUI gate.

    Returns the pair left on screen (None when the last accepted effect was a
    clear) and the per-effect verdicts as ``(kind, pair, accepted)``.
    """

    import overlay_paint_window

    gate = overlay_paint_window.GenerationGate()
    on_screen: Optional[tuple[int, int]] = None
    verdicts = []
    for e in effects:
        pair = (e.overlay_session_id, e.paint_generation)
        if e.kind is EffectKind.DISPATCH_PAINT:
            ok = gate.accept_paint(*pair)
            if ok:
                on_screen = pair
        elif e.kind is EffectKind.DISPATCH_CLEAR:
            ok = gate.accept_clear(*pair)
            if ok:
                on_screen = None
        else:
            continue
        verdicts.append((e.kind, pair, ok))
    return on_screen, verdicts


def test_new_walk_paint_after_restart_clear_passes_the_gui_gate():
    """wh-overlay-failed-focus-read-keeps-numbers.1.1: the restart clear is
    stamped with the OLD pair, so the new read's paint at the bumped pair
    still presents. A clear at the new pair would refuse it."""

    machine = ClickOverlayStateMachine()
    stream = list(machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS)).effects)
    sid = machine.overlay_session_id
    stream += machine.apply(OverlayEvent(
        OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
        paint_generation=machine.paint_generation, snapshot_id="snap-w1",
    )).effects
    stream += machine.apply(OverlayEvent(OverlayEventKind.FOCUS_CHANGE)).effects
    stream += machine.apply(OverlayEvent(
        OverlayEventKind.BUILD_RESPONSE, overlay_session_id=sid,
        paint_generation=machine.paint_generation, snapshot_id="snap-w2",
    )).effects
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    on_screen, verdicts = _replay_through_gui_gate(stream)
    assert on_screen == (sid, machine.paint_generation)
    assert [k for k, _p, _ok in verdicts] == [
        EffectKind.DISPATCH_PAINT, EffectKind.DISPATCH_CLEAR,
        EffectKind.DISPATCH_PAINT,
    ]
    assert all(ok for _k, _p, ok in verdicts)


def _pin_response(action, params) -> dict:
    from services.wheelhouse.shared.pin_snapshot import PinSnapshotResponse

    return PinSnapshotResponse(
        status="ok",
        reason=None,
        overlay_session_id=int(params.get("overlay_session_id", 0)),
        snapshot_id=str(params.get("snapshot_id", "")),
        pinned=action == "pin_snapshot",
    ).to_dict()


# ---------------------------------------------------------------------------
# PART 3: the 200ms hold-or-drop timer.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PART 4: summary-cache keepalive past the 30s TTL for a visible overlay.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PART 5: tracked identity cleared on entry to closed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FINDING 2 (wh-n29v.96.2): keepalive must slide for a quiescent painted
# overlay. PAINTED is a steady NO_TIMEOUT state with no recurring pin/paint, so
# a periodic keepalive timer must re-put the visible summary while painted, or a
# >TTL idle dwell loses the summary and 'click N' misreports 'no badge N'.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FINDING 3 (wh-n29v.96.3): a held 'click N' must not hit the wrong overlay.
# The held click carries the (session, generation) armed at hold time; if a
# supersede / new session reaches a resolvable state within the 200ms hold, the
# armed pair no longer matches the machine and the click must NOT fire.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FINDING 4 (wh-n29v.96.4): the synchronous BUILD_RESPONSE feed must be deferred
# so the in-flight walk batch fully drains (including its trailing ARM_TIMER)
# and releases the lock BEFORE the build-response transition schedules its
# effects. Otherwise a stale WALK-duration timer is briefly armed at the
# still-current generation.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reviewer_1 (wh-n29v.97) findings .2 and .3
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Post-click refresh feed (this slice): a successful numbered-overlay item
# click must feed an OverlayEvent(CLICK_COMPLETE) so a painted overlay
# refreshes (painted -> refresh_in_flight, generation bumped) instead of
# staying pinned to the pre-click UI. The state machine already implements
# painted + CLICK_COMPLETE -> _refresh; this verifies the Logic side feeds it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Post-click refresh deferral (wh-n29v.101.1): a click ok that arrives while the
# overlay is REFRESH_IN_FLIGHT cannot refresh immediately (the machine returns
# HELD for CLICK_COMPLETE in that state, and the captured dispatch pair already
# names the in-flight generation). The Logic side must record a PENDING
# post-click refresh keyed on that pair and replay CLICK_COMPLETE once the SAME
# generation settles into PAINTED, so the next 'click N' resolves against a
# fresh post-click snapshot rather than the pre-click one. The pending refresh
# is cleared on supersede (the live pair moves past the recorded one) and on
# session end (entry to closed) so a stale pending refresh never fires on a
# newer overlay.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.2: a click reply that lands while the
# machine is PAUSED at the matching dispatch pair must DEFER the post-click
# refresh, exactly like the REFRESH_IN_FLIGHT branch, not drop it. The pause
# lands between the click dispatch and its reply, so the refresh the click
# earned would otherwise disappear and the resume restore would repaint the
# PRE-CLICK list with no fresh walk behind it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Deferral robustness (wh-n29v.102.1 / .2 / .3): the pending pair is left SET
# at schedule time and the deferred callback owns the consume/keep/drop
# decision, so a mic-pause landing between the schedule and the callback fire
# cannot lose the refresh. These tests drive the machine's mic-pause/mic-resume
# transitions directly through _apply_overlay_event; the integration does not
# yet feed those events into the overlay machine (a later slice), so the PAUSED
# paths are exercised here to lock the deferral logic for when it does.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.15: a focus refresh that lands between a
# badge click and its Input reply must not bypass the post-click clear.
#
# The interleave: the click is dispatched from PAINTED and captures that pair.
# One of the four FOCUS_CHANGE sources applies _refresh before the Input reply,
# which bumps the generation, so _feed_click_complete_refresh's gate 1 sees a
# mismatched pair. Before this bead the reply was dropped there, and a refresh
# that then FAILED put the PRE-CLICK badges back on screen over a screen the
# click had already changed.
#
# The two legs are told apart by snapshot identity, not by timing (boss ruling
# 2026-08-27, Option C): after the intervening refresh reaches PAINTED, the pin
# still holding the PRE-CLICK snapshot id means the refresh fell back, so the
# clear must fire; a different pin means a fresh list is on screen, so it must
# not.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-open on ambiguous match (wh-n29v.111 / source leaf wh-ynr5zb).
#
# When click_element returns outcome="ambiguous" with ambiguous_item_ids, and
# the overlay is enabled AND overlay_auto_open_on_ambiguous is True AND the
# machine is in CLOSED, forward_click_element feeds an AUTO_OPEN OverlayEvent
# (carrying the suppressed notice + the reuse snapshot_id) into the state
# machine and SUPPRESSES today's immediate click notice. The auto-open build
# dispatches show_numbered_overlay restricted to the ambiguous finalists via
# item_id_filter (sourced from response.ambiguous_item_ids), and the Logic-side
# cached WalkSnapshotSummary is REPLACED with the filtered renumbered subset the
# Input handler returns. On any auto-open failure path (build-fail, paint-fail,
# walk timeout, paint timeout) the state machine fires the suppressed notice
# once (r2.9). When the overlay is disabled, auto-open is disabled, or the
# dispatch does not enter the machine, forward_click_element falls back to the
# plain notice path unchanged.
# ---------------------------------------------------------------------------


def _ambiguous_response(
    *, snapshot_id="snap-amb", item_ids=("snap-amb-item-1", "snap-amb-item-2"),
    matched_names=("Cancel", "Cancel all"), trace_id="tr-amb",
) -> dict:
    return ClickElementResponse(
        status="error",
        outcome="ambiguous",
        reason=None,
        matched_names=matched_names,
        snapshot_id=snapshot_id,
        snapshot_summary=_summary(snapshot_id, 1, 2, 3),
        matched_name=None,
        trace_id=trace_id,
        ambiguous_item_ids=item_ids,
    ).to_dict()


def _forward_controller(
    *, overlay_effective=True, auto_open=True, send_result=None,
    machine=None, cache=None,
):
    """A controller wired for forward_click_element auto-open tests.

    Reuses the integration ``_controller`` helper (so the overlay attributes are
    injected) and binds the few extra attributes ``forward_click_element``
    touches: the [click] config knobs and the fake app. The first-use hint
    finds no foreground window (the ``_no_foreground_window`` fixture), so
    it shows nothing. ``send_result`` is the dict ``app.send_request``
    resolves to.
    """

    controller = _controller(
        overlay_effective=overlay_effective, machine=machine, cache=cache,
    )
    # ``_controller`` set ``click_config`` to a MagicMock; pyright still sees the
    # declared ``ClickConfig`` (a frozen dataclass with read-only attributes)
    # once narrowing is lost across the helper boundary, so set the extra knobs
    # through an ``Any`` cast. At runtime these are plain MagicMock attribute
    # writes.
    cfg = cast(Any, controller.click_config)
    cfg.overlay_auto_open_on_ambiguous = auto_open
    cfg.enable_screen_reader_flag = False
    controller._click_disabled_notice_shown = False
    controller.background_tasks = []

    def _respond(action, params):
        # ``send_result`` is the click_element reply (the ambiguous response).
        # Every overlay build/pin action gets a VALID generation-aware reply so a
        # default-wired controller can drive the auto-open round trip without a
        # malformed show_numbered_overlay payload.
        if action == "click_element":
            return send_result
        if action == "show_numbered_overlay":
            sid = params.get("overlay_session_id", 0)
            gen = params.get("paint_generation", 0)
            snap = params.get("snapshot_id", "snap-amb") or "snap-amb"
            return _show_response(snap, sid, gen, 1, 2)
        return _pin_response(action, params)

    _wire_app(controller, _respond)
    return controller


def _amb_query():
    from services.wheelhouse.ui.element_types import ElementQuery

    return ElementQuery("cancel", "Button", None, None, "cancel")


def test_ambiguous_auto_open_applies_event_and_suppresses_notice():
    """Outcome=ambiguous with overlay+auto-open on and CLOSED machine: an
    AUTO_OPEN OverlayEvent is applied (notice stashed, reuse snapshot carried)
    and the immediate notice path is NOT taken."""

    async def _run():
        controller = _forward_controller(send_result=_ambiguous_response())
        controller.loop = asyncio.get_running_loop()
        machine = controller.overlay.click_overlay_state
        # No immediate notice: the standalone notice path must be suppressed.
        controller.overlay.forward_click_notice = MagicMock()  # type: ignore[method-assign]
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # The machine left CLOSED via AUTO_OPEN and the suppressed notice is held in
    # pending_ambiguous_notice (until painted clears it or a failure fires it).
    assert machine.state is not OverlayState.CLOSED
    assert machine.pending_ambiguous_notice is not None
    assert machine.pending_ambiguous_notice.outcome == "ambiguous"
    assert machine.pending_ambiguous_notice.matched_names == (
        "Cancel", "Cancel all",
    )
    # The immediate notice path was suppressed (no show_click_notice forwarded).
    cast(MagicMock, controller.overlay.forward_click_notice).assert_not_called()
    # show_numbered_overlay was dispatched (auto-open build), NOT a notice.
    sent_actions = [a for a, _ in controller._sent]  # type: ignore[attr-defined]
    assert "show_numbered_overlay" in sent_actions


def test_ambiguous_auto_open_dispatches_filter_snapshot_and_generation():
    """The auto-open build dispatches show_numbered_overlay with item_id_filter
    == list(response.ambiguous_item_ids), snapshot_id == the response snapshot,
    and the machine's (overlay_session_id, paint_generation)."""

    async def _run():
        item_ids = ("snap-amb-item-1", "snap-amb-item-2")
        amb = _ambiguous_response(item_ids=item_ids)

        def _respond(action, params):
            if action == "click_element":
                return amb
            if action == "show_numbered_overlay":
                sid = params["overlay_session_id"]
                gen = params["paint_generation"]
                # The handler filters + renumbers to 1..K; echo a 2-item summary.
                return _show_response("snap-amb", sid, gen, 1, 2)
            return _pin_response(action, params)

        controller = _forward_controller(send_result=amb)
        controller.loop = asyncio.get_running_loop()
        # Re-wire the app to a generation-aware responder.
        _wire_app(controller, _respond)
        machine = controller.overlay.click_overlay_state
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine, item_ids

    controller, machine, item_ids = asyncio.run(_run())
    show = [
        (a, p) for a, p in controller._sent  # type: ignore[attr-defined]
        if a == "show_numbered_overlay"
    ]
    assert len(show) == 1
    params = show[0][1]
    assert params["snapshot_id"] == "snap-amb"
    assert params["item_id_filter"] == list(item_ids)
    assert params["overlay_session_id"] == machine.overlay_session_id


@pytest.mark.parametrize("overlay_effective,auto_open", [(False, True), (True, False)])
def test_ambiguous_falls_back_to_notice_when_disabled(overlay_effective, auto_open):
    """overlay disabled OR auto-open disabled: the ambiguous outcome takes the
    plain notice path -- no auto-open, no suppression."""

    async def _run():
        controller = _forward_controller(
            overlay_effective=overlay_effective, auto_open=auto_open,
            send_result=_ambiguous_response(),
        )
        controller.loop = asyncio.get_running_loop()
        machine = controller.overlay.click_overlay_state
        controller.overlay.forward_click_notice = MagicMock()  # type: ignore[method-assign]
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    # No auto-open: the machine stays CLOSED and no overlay build was dispatched.
    assert machine.state is OverlayState.CLOSED
    sent_actions = [a for a, _ in controller._sent]  # type: ignore[attr-defined]
    assert "show_numbered_overlay" not in sent_actions
    # The plain ambiguous notice was forwarded.
    notice = cast(MagicMock, controller.overlay.forward_click_notice)
    notice.assert_called_once()
    _, kwargs = notice.call_args
    assert kwargs.get("outcome") == "ambiguous"


def test_ambiguous_falls_back_to_notice_when_no_item_ids():
    """An ambiguous response missing ambiguous_item_ids cannot drive a restricted
    auto-open, so it falls back to the plain notice path."""

    async def _run():
        controller = _forward_controller(
            send_result=_ambiguous_response(item_ids=None),
        )
        controller.loop = asyncio.get_running_loop()
        machine = controller.overlay.click_overlay_state
        controller.overlay.forward_click_notice = MagicMock()  # type: ignore[method-assign]
        await controller.forward_click_element(_amb_query(), "tr-amb")
        await _settle(controller)
        return controller, machine

    controller, machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    cast(MagicMock, controller.overlay.forward_click_notice).assert_called_once()


# ---------------------------------------------------------------------------
# wh-n29v.117: the floating-button "walking" progress cue emit.
#
# ``_overlay_dispatch_build`` is the single funnel every walk-start passes
# through. It must enqueue a plain-dict ``overlay_walk_cue`` action onto
# state_to_gui_queue: active:True at walk-start (before the send_request) and
# active:False at BOTH the build-success feed path AND the build-failure /
# timeout feed path. This rides the existing GUI state queue (no new shared/
# schema, no new EffectKind) and the GUI consumer is defensive.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-n29v.121: proactive refresh while PAINTED over a browser window. Cached
# badge positions go stale fast on dynamic Chromium pages; the keepalive tick
# feeds one FOCUS_CHANGE (the same event the focus/menu hooks reuse, mapping
# to a REFRESH in PAINTED) once the overlay has been painted longer than
# overlay_browser_refresh_seconds AND the tracked window is a browser/Electron
# process. Non-browser windows, sub-window ages, a zero window (opt-out), a
# missing tracked identity, and any non-PAINTED state must all skip.
# ---------------------------------------------------------------------------


async def _drive_browser_refresh_case(
    *,
    process_name: str = "brave.exe",
    window_s: int = 10,
    advance_s: float = 15.0,
    identity_none: bool = False,
):
    """Paint over a window of ``process_name``, then let time pass.

    The overlay is painted through 'show numbers' and its GUI paint ack;
    the keepalive timer then fires because the loop clock moves
    ``advance_s`` seconds. ``identity_none`` paints with no foreground
    window, so the overlay tracks no window. Returns ``(state, outputs,
    renews)``: the machine state after the tick, the requests sent to Input
    from the tick on, and the GUI lease renews of the tick (proof that the
    tick ran).
    """

    window = Window(
        hwnd=0x4B01, pid=4201, process_name=process_name,
        class_name="Chrome_WidgetWin_1", create_time_s=1_700_000_100.0,
    )
    raw = {
        "overlay_browser_refresh_seconds": window_s,
        "browser_processes": ["brave.exe", "chrome.exe"],
        "browser_processes_extend": [],
    }
    async with _rig(raw, windows=(window,), offset_clock=True) as rig:
        rig.desktop.foreground = 0 if identity_none else window.hwnd
        await rig.paint("snap-br", (1, 2))
        mark = rig.mark()
        await rig.clock.advance(advance_s)
        await settle()
        sent = [
            action for kind, action, _p in rig.log[mark:] if kind == "input"
        ]
        renews = rig.payloads("overlay_lease_renew", mark)
        return rig.machine.state, sent, renews


def _refresh_walks(sent) -> list:
    return [a for a in sent if a == "start_overlay_walk"]


def test_browser_refresh_process_name_matching_is_case_folded():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="Brave.EXE", window_s=10, advance_s=15.0,
        )

    _state, sent, renews = asyncio.run(_run())
    assert len(renews) == 1
    assert len(_refresh_walks(sent)) == 1


def test_browser_refresh_disabled_when_window_zero():
    async def _run():
        return await _drive_browser_refresh_case(
            process_name="brave.exe", window_s=0, advance_s=3600.0,
        )

    state, sent, renews = asyncio.run(_run())
    assert len(renews) == 1
    assert _refresh_walks(sent) == []
    assert state is OverlayState.PAINTED


def test_browser_refresh_skips_without_tracked_identity():
    async def _run():
        return await _drive_browser_refresh_case(
            identity_none=True, window_s=10, advance_s=60.0,
        )

    state, sent, renews = asyncio.run(_run())
    assert len(renews) == 1
    assert _refresh_walks(sent) == []
    assert state is OverlayState.PAINTED


# ---------------------------------------------------------------------------
# wh-overlay-fixqueue-review.1: failed proactive refreshes back off.
#
# A failed refresh restores the prior snapshot by re-entering PAINTED, which
# re-stamps the paint age -- so before this, a window whose walk consistently
# failed was re-walked every window with no back-off, stalling the Input
# process's serial command loop each time. Now each failed PROACTIVE refresh
# doubles the effective trust window (capped), a successful one resets it,
# and closing the session resets it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.4: the renumber guard arms for EVERY read.
#
# Badge numbers are positional (uia_walker.py increments display_number in
# walk order), so one control added or removed anywhere renumbers everything
# after it. The guard that catches a "click N" spoken across a repaint used to
# arm only for the browser timer's proactive refresh. These tests drive the
# two other repaints that replace the painted list -- a focus-triggered
# refresh and the post-click settle re-read -- over a NON-browser process, and
# the two restores that must NOT arm it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config -> state machine wiring (wh-overlay-slow-uia-stale-badges.1)
# ---------------------------------------------------------------------------


def test_overlay_machine_takes_the_settle_flag_from_config():
    """LogicController must not construct the machine at a hard-coded flag.

    The flag ships False. This test proves the config value reaches the
    machine, so turning the key on in config.toml is enough to enable the
    behaviour once child .2 lands.
    """
    from services.wheelhouse.numbered_overlay.helpers import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    on = ClickConfig.from_raw(
        {"enabled": True, "overlay_settle_after_click": True}
    )
    off = ClickConfig.from_raw({"enabled": True})

    assert make_click_overlay_state_machine(on).settle_after_click is True
    assert make_click_overlay_state_machine(off).settle_after_click is False


def test_overlay_machine_deadlines_follow_the_screen_read_key():
    """The factory derives both walk-ish deadlines from the read limit.

    wh-overlay-slow-uia-stale-badges.3. The settle deadline is the
    load-bearing one: at the class default of 8000 a settle read that answers
    at 9 s would be dropped by POST_CLICK_SETTLING's own timeout while the
    Logic awaiter still had a second left, which is the same discard the bead
    removes elsewhere. Both carry the 250 ms pre-walk margin so the machine
    deadline outlasts the Input-side read it is waiting on. The settle one
    also carries ``_OVERLAY_READ_SENTENCE_WAIT_MAX_S`` in milliseconds
    (wh-overlay-slow-uia-stale-badges.3.1.1): its timer is armed BEFORE the
    settle build, and that build waits for the open dictation sentence to end
    before the read is even sent, so without the wait term a read still inside
    its own bound is discarded by the state. ``walk_deadline_ms`` does not get
    the term -- those timers are armed after the build round trip, so the wait
    is already inside their window. ``settle_deadline_ms`` never drops below
    the class default, and ``paint_deadline_ms`` is untouched (a paint is a
    GUI round trip, not a read).
    """
    from services.wheelhouse.numbered_overlay.helpers import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    wait_ms = int(_OVERLAY_READ_SENTENCE_WAIT_MAX_S * 1000)

    m = make_click_overlay_state_machine(
        ClickConfig.from_raw({"enabled": True, "screen_read_timeout_ms": 8000})
    )
    assert m.walk_deadline_ms == 8250
    assert m.settle_deadline_ms == 8250 + wait_ms == 13250

    default = make_click_overlay_state_machine(
        ClickConfig.from_raw({"enabled": True})
    )
    assert default.walk_deadline_ms == 10250
    assert default.settle_deadline_ms == 10250 + wait_ms == 15250
    assert default.paint_deadline_ms == 1000


def _click_complete_effects_for(raw_click_config, *, answer_walk_as=None):
    """Drive a real successful badge click and return what it sent.

    The overlay is built through its public constructor from a validated
    ClickConfig, so the assertion covers the whole path from the config key
    to the outputs, not just the machine in isolation
    (wh-overlay-slow-uia-stale-badges.13.4). The numbers are painted through
    'show numbers', the click is the spoken "click 2", and the walk the click
    starts is held unanswered. Returns ``(machine fields, pair, outputs,
    log, expiry)``: ``outputs`` are the overlay outputs from the click on,
    ``log`` the whole rig log from the click on, and ``expiry`` what the
    state's own timer did.

    ``expiry`` moves the clock to half a second before the deadline of the
    state the click left the machine in, then to half a second after it.
    The rig's send has no time limit, and no paint is acknowledged, so only
    the timer the click armed can move the machine: ``expiry.before`` and
    ``expiry.after`` are the machine fields at the two instants, and
    ``expiry.outputs`` and ``expiry.log`` the overlay outputs and the rig log
    from the first instant on. With ``answer_walk_as`` the held walk is first
    answered with that snapshot id (``expiry.answered`` holds the outputs of
    that answer); without it the walk is never answered.
    """

    async def _run():
        async with _rig(raw_click_config, offset_clock=True) as rig:
            sid, gen = await rig.paint("snap-cc", (1, 2, 3))
            rig.hold_walks = True
            mark = rig.mark()
            await rig.say_click(2, "tr-cc")
            clicked = rig.snapshot()
            outputs, log = rig.outputs(mark), rig.log[mark:]
            # The display acknowledges a clear the click sent, as the GUI
            # does, so the clear-ack watchdog has nothing to report when the
            # clock moves.
            for clear in _logged(log, "clear_overlay"):
                await rig.ack(clear, state="cleared")
            answered = None
            if answer_walk_as is not None:
                answer_mark = rig.mark()
                rig.release(0, rig.walk_reply(rig.held[0][0], answer_walk_as))
                await settle()
                answered = rig.log[answer_mark:]
            deadline_s = rig.machine.timeout_ms(clicked.state) / 1000.0
            await rig.clock.advance(deadline_s - 0.5)
            before = rig.snapshot()
            expiry_mark = rig.mark()
            await rig.clock.advance(1.0)
            expiry = types.SimpleNamespace(
                answered=answered, before=before, after=rig.snapshot(),
                outputs=rig.outputs(expiry_mark), log=rig.log[expiry_mark:],
            )
            return clicked, (sid, gen), outputs, log, expiry

    return asyncio.run(_run())


def _logged(log, action) -> list:
    return [payload for _kind, logged, payload in log if logged == action]


def test_click_complete_effects_reach_the_performer_with_the_flag_off():
    """The settle-off path: a successful click dispatches a refresh build.

    wh-numbers-stay-setting: the refresh after a click is now the behaviour of
    ``overlay_keep_numbers`` true; the default (false) removes the numbers, and
    the tests named ``test_overlay_keep_numbers_*`` cover that.
    """
    machine, (sid, gen), outputs, log, expiry = _click_complete_effects_for(
        {"enabled": True, "overlay_keep_numbers": True},
        answer_walk_as="snap-cc-after",
    )

    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert machine.overlay_session_id == sid
    assert machine.paint_generation == gen + 1
    # The click, then the refresh build; nothing cleared, nothing unpinned.
    assert outputs == [
        ("input", "click_snapshot_item"),
        ("input", "start_overlay_walk"),
    ]
    clicks = _logged(log, "click_snapshot_item")
    assert clicks[0]["snapshot_id"] == "snap-cc"
    assert clicks[0]["item_id"] == "snap-cc-item-2"
    walk = _logged(log, "start_overlay_walk")[0]
    assert (walk["overlay_session_id"], walk["paint_generation"]) == (
        sid, gen + 1,
    )
    # Still visible, so still pinned.
    assert machine.pinned_snapshot_id == "snap-cc"
    # The refresh's own timer: armed for REFRESH_IN_FLIGHT at the refresh's
    # generation once the walk answers, so a refresh paint that is never
    # acknowledged falls back at its deadline, and not before: the new
    # snapshot is unpinned and the painted numbers stay pinned.
    painted = _logged(expiry.answered, "paint_overlay")
    assert [p["snapshot_id"] for p in painted] == ["snap-cc-after"]
    assert expiry.before.state is OverlayState.REFRESH_IN_FLIGHT
    assert expiry.after.state is OverlayState.PAINTED
    assert expiry.after.pinned_snapshot_id == "snap-cc"
    assert expiry.outputs == [("input", "unpin_snapshot")]
    assert _logged(expiry.log, "unpin_snapshot")[0]["snapshot_id"] == (
        "snap-cc-after"
    )


def test_click_complete_effects_reach_the_performer_with_the_flag_on():
    """The gated path: the same click clears the badges and arms the settle.

    This is the only test that runs the config key, the factory, the
    controller feed and the effect contract together. The machine-level
    settling tests build ClickOverlayStateMachine(settle_after_click=True)
    by hand, which cannot catch a factory or feed that drops the flag.
    """
    machine, (sid, gen), outputs, log, expiry = _click_complete_effects_for(
        {
            "enabled": True,
            "overlay_settle_after_click": True,
            # wh-numbers-stay-setting: false (the default) wins over settle.
            "overlay_keep_numbers": True,
        }
    )

    assert machine.state is OverlayState.POST_CLICK_SETTLING
    # The click, the clear of the painted badges, then the settle build.
    assert outputs == [
        ("input", "click_snapshot_item"),
        ("gui", "clear_overlay"),
        ("input", "start_overlay_walk"),
    ]
    # The pin and the SESSION survive: child .2 compares its next read
    # against the list that was on screen, and repaints into this same
    # session. The GENERATION moves, because child .2 spends the bump child
    # .1 reserved on the settle build it dispatches here; the clear keeps
    # the generation the GUI painted.
    assert machine.pinned_snapshot_id == "snap-cc"
    assert machine.overlay_session_id == sid
    clear = _logged(log, "clear_overlay")[0]
    assert (clear["overlay_session_id"], clear["paint_generation"]) == (
        sid, gen,
    )
    walk = _logged(log, "start_overlay_walk")[0]
    assert (walk["overlay_session_id"], walk["paint_generation"]) == (
        sid, gen + 1,
    )
    assert machine.paint_generation == gen + 1
    # The settle's own timer: armed for POST_CLICK_SETTLING at the bumped
    # generation, so its TIMEOUT passes the machine's generation gate
    # (wh-overlay-slow-uia-stale-badges.2.2.3). At the deadline the settle
    # that never answered gives up, unpins and closes, and not before.
    assert expiry.before.state is OverlayState.POST_CLICK_SETTLING
    assert expiry.after.state is OverlayState.CLOSED
    assert expiry.after.pinned_snapshot_id is None
    assert ("input", "unpin_snapshot") in expiry.outputs


# ---------------------------------------------------------------------------
# Concurrent badge clicks (wh-overlay-slow-uia-stale-badges.8).
#
# Every resolved badge click runs as an independent background task and the
# machine stays PAINTED until the Input reply arrives, so before this slice a
# second spoken number could resolve against the SAME old list and be sent
# while the first click was still in flight; Input runs both in order, and
# the second click landed after the screen the user read was gone. Four
# guards close that window:
#
#   1. A badge click carries the (overlay_session_id, paint_generation) pair
#      of the VISIBLE list it was resolved against (the machine's current
#      pair in PAINTED; the pair of the last successful paint during
#      REFRESH_IN_FLIGHT), tracked by
#      ``_reconcile_overlay_visible_painted_pair``.
#   2. While one badge click is in flight, a second one is REFUSED with an
#      overlay_click_in_flight notice (never silence, never a second send).
#   3. A successful click cancels a held 'click N' armed at the same pair
#      (the only not-yet-sent click Logic queues) with a numbers_updating
#      notice instead of letting it resolve against the consumed list.
#   4. Input refuses a click whose resolved-against pair is not newer than
#      the last EXECUTED click's pair (stale_overlay_generation) -- the
#      Input-side tests live in test_ui/test_click_snapshot_item_handler.py;
#      the reason's membership in the rewalk set is asserted here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review follow-ups (wh-overlay-slow-uia-stale-badges.16).
#
# .16.1: a FAILED refresh falls back to PAINTED with the machine's bumped
# generation but the SAME still-visible list (the fall-back restores the
# prior pin, not the prior generation). Recording the bumped pair there
# would re-admit the consumed list under a new generation and defeat the
# Input watermark: the next click on the same physical list would carry a
# pair strictly newer than the watermark and EXECUTE against a screen the
# first click already changed. The reconciler therefore memoizes WHICH
# pinned snapshot its recorded pair belongs to, and on PAINTED entry keeps
# the pair when the machine's pin is that same snapshot (fall-back restore,
# paused-resume repaint) -- recording only for a genuinely fresh list.
#
# .16.3: coverage gaps -- the REFRESH_IN_FLIGHT payload/dispatch-pair
# divergence, the hold timer firing mid-flight, the GUI (mouse) badge-click
# path against the new guards, and the two untested in-flight-marker exits.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.9: the pending-clear watchdog (Shape A),
# the GUI badge-lease renew (Shape B, Logic side), the expired ack, and the
# startup reset. Logic must REPORT a lost clear, not stay silent; the GUI
# lease is the remover.
# ---------------------------------------------------------------------------


def test_expired_ack_when_closed_is_warning_bookkeeping(caplog):
    """An expired ack when the machine is not PAINTED at that pair is
    bookkeeping (the lease cleaned up after a clear whose loss the 5s
    watchdog already reported): WARNING, no popup, no state change."""

    async def _run():
        controller = _controller()  # machine starts CLOSED at pair (0, 0)
        controller.loop = asyncio.get_running_loop()
        controller.background_tasks = []
        _wire_app(controller, _pin_response)

        from services.wheelhouse.shared.overlay_state_changed import (
            OverlayStateChangedEvent,
        )

        ack = OverlayStateChangedEvent(
            state="expired", overlay_session_id=0, paint_generation=0,
        ).to_dict()
        await controller.overlay.handle_overlay_state_changed(ack)
        await _settle(controller)
        return controller.overlay.click_overlay_state

    with caplog.at_level(logging.WARNING, logger="services.wheelhouse.numbered_overlay.controller"):
        machine = asyncio.run(_run())
    assert machine.state is OverlayState.CLOSED
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("lease" in r.getMessage() for r in warnings)


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.19: the refresh fall-back is a rollback
# boundary, so the two cross-process sends it races against must be fenced at
# the point of COMMIT -- after the await, not only before it.
#
#   * the build dispatcher must not feed a BUILD_RESPONSE the machine can no
#     longer use. ``_on_painted`` has no build-response cell, so the feed drove
#     the machine to ERROR with the prior badges still on screen, and "click N"
#     then answered numbers_not_showing while the user was looking at numbers.
#   * the paint dispatcher must not present a generation the fall-back already
#     abandoned. The pin ack await postpones the paint, so the screen would
#     show the NEW list while Logic resolves "click N" against the RESTORED
#     prior snapshot.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.4: every paint-dispatch path that
# enqueues NO paint must clear the walking cue itself. The GUI clears the cue
# when paint_overlay (or clear_overlay) arrives, so a drop that sends neither
# leaves the moving dot on screen until the GUI fallback timer expires (about
# 6 s by default) -- the dot claims a walk is in flight after the walk ended.
# Two such paths exist: the commit fence and the summary-cache miss.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.3: the paused-resume restore paint is the
# only paint that commits PAINTED with no ack-expecting state behind it, so a
# lost restore paint leaves Logic resolving "click N" against a list the user
# cannot see. The effect carries ``audit_delivery``; the integration audits it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.5: the audit must also cover the time the
# restore paint waits for ``_overlay_effect_lock``. ``_apply_overlay_event``
# commits PAINTED synchronously and only SCHEDULES the effects, while an older
# batch holds the lock across every awaited pin and unpin (up to
# ``response_timeout_ms`` each). Arming the audit at enqueue time alone left
# that whole wait unaudited.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.8: an exit that keeps BOTH the pair and
# the pin supersedes an audited restore paint that is still waiting for
# ``_overlay_effect_lock``. The ordinary paint fence deliberately reads only
# the pair and the pin, so it passes that superseded paint and the user sees a
# frame of badges the machine no longer wants. The schedule-time audit is left
# armed as well, and it reports a user-visible loss for an overlay the machine
# has already taken off the screen.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.21.6: ``_recover_lost_restore_paint`` marks
# the pair as already-reported BEFORE it defers the synthetic hide. When the
# deferred half re-validates and does nothing, that marker outlives a clear
# that never existed, and it then downgrades a later genuine clear fault at the
# same pair from an ERROR the user sees to a WARNING only the log holds.
# ---------------------------------------------------------------------------


# The second window of the settle tests: the foreground moves to it while
# the window the click changed settles.
_RIG_OTHER_WINDOW = Window(
    hwnd=0x4A02, pid=4102, process_name="otherapp.exe",
    class_name="OtherMainWindow", create_time_s=1_700_000_050.0,
)

_SETTLE_RAW = {
    "enabled": True, "overlay_settle_after_click": True,
    # wh-numbers-stay-setting: false (the default) wins over settle.
    "overlay_keep_numbers": True,
}


def _run_settle_build(walk_snapshot_id: str, *, foreground_moves: bool = False):
    """Click a painted badge, then answer the SETTLE read and let it finish.

    The overlay is painted over ``_RIG_WINDOW`` (snapshot "snap-pre"), the
    spoken "click 2" succeeds, and the machine enters post_click_settling
    and sends the settle walk. The walk reply names ``walk_snapshot_id`` and
    the window Input READ, which is ``_RIG_WINDOW``. ``foreground_moves``
    brings ``_RIG_OTHER_WINDOW`` to the front before the reply lands
    (wh-overlay-slow-uia-stale-badges.2.2.1). Returns ``(machine fields,
    log)``, the log taken from the click on.
    """

    async def _run():
        async with _rig(
            _SETTLE_RAW, windows=(_RIG_WINDOW, _RIG_OTHER_WINDOW),
        ) as rig:
            await rig.paint("snap-pre", (1, 2))
            rig.hold_walks = True
            mark = rig.mark()
            await rig.say_click(2, "tr-settle")
            assert rig.machine.state is OverlayState.POST_CLICK_SETTLING
            assert len(rig.held) == 1, "the settle build must reach Input"
            if foreground_moves:
                rig.desktop.bring_to_front(_RIG_OTHER_WINDOW)
            params = rig.held[0][0]
            rig.release(0, rig.walk_reply(
                params, walk_snapshot_id, (1, 2), read_from=_RIG_WINDOW,
            ))
            await settle()
            return rig.snapshot(), rig.log[mark:]

    return asyncio.run(_run())


def test_the_settle_reply_reaches_the_machine_that_is_still_settling():
    """The commit fence must admit post_click_settling, or the badges never return.

    ``_overlay_dispatch_build._feed`` re-checks the machine at the commit point
    and drops a reply the machine can no longer use
    (wh-overlay-slow-uia-stale-badges.19). Its list of states that CAN still use
    a reply was written when only walk_in_flight and refresh_in_flight had a
    BUILD_RESPONSE cell that does work. This bead added a third:
    ``_on_post_click_settling`` repaints the held pin or paints the new
    snapshot. The settle build is dispatched FROM post_click_settling and the
    machine is still there when the reply lands, at the SAME
    (overlay_session_id, paint_generation) pair -- so a fence that omits the
    state drops every settle reply, and the state closes on its own timer with
    no repaint. That is the whole feature failing, and it is invisible to a
    machine-level test, which never runs the fence.

    Found by deepseek round 1 as wh-overlay-slow-uia-stale-badges.2.1.1.
    """
    machine, log = _run_settle_build("snap-pre")

    walks = _logged(log, "start_overlay_walk")
    assert len(walks) == 1, "the settle build must still reach Input"
    # The reply was CONSUMED: the unchanged answer repaints the held pin, which
    # is the paint_in_flight transition. A dropped reply leaves the machine
    # sitting in post_click_settling until its timer fires.
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snap-pre"


def test_a_changed_settle_reply_also_reaches_the_still_settling_machine():
    """The other leg of the same fence: a DIFFERENT snapshot id must land too.

    The unchanged leg and the changed leg arrive through the identical fence,
    so a fix that admitted only one of them would be no fix at all. Here the
    settled read answers with a new snapshot, and the machine must unpin the
    pre-click snapshot, pin the new one and paint it.
    """
    machine, log = _run_settle_build("snap-after")

    assert len(_logged(log, "start_overlay_walk")) == 1
    assert machine.state is OverlayState.PAINT_IN_FLIGHT
    assert machine.pinned_snapshot_id == "snap-after"


# ---------------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.2.2.1: the settle reply describes ONE
# window, and the foreground can move while the window settles. The commit
# fence revalidates the reply's window against the current foreground before
# the machine can pin or paint it.
# ---------------------------------------------------------------------------


def test_a_settle_reply_for_another_window_closes_instead_of_painting():
    """The badges read from window A must never be painted over window B.

    ``post_click_settling``'s FOCUS_CHANGE cell is a deliberate NO_OP: a click
    that opens a dialog CHANGES the foreground, and that is the common case
    for 'click N', so the machine must not cancel on it. The cell's comment
    delegates the safety to the integration -- "the integration picks the
    window to read and revalidates its identity before it paints". This is
    that revalidation.

    Input reads window 1 and reports it. By the time the reply reaches the
    commit fence the foreground is window 2, so the numbers describe controls
    that are no longer on screen. The reply must NOT be painted; the overlay
    fails closed, exactly as the state's own TIMEOUT arm does, and the user
    says 'show numbers' again.
    """
    machine, log = _run_settle_build("snap-after", foreground_moves=True)

    walks = _logged(log, "start_overlay_walk")
    assert len(walks) == 1, "the settle build still reaches Input"
    paints = _logged(log, "paint_overlay")
    assert paints == [], (
        "a reply describing window 1 was painted while window 2 is foreground"
    )
    assert machine.state is OverlayState.CLOSED, (
        "the overlay must fail closed on a foreground-identity mismatch"
    )
    assert machine.pinned_snapshot_id is None, (
        "failing closed must not strand the pre-click pin"
    )


# ----------------------------------------------------------------------
# wh-overlay-slow-uia-stale-badges.24: the four remaining supersede paths
# ----------------------------------------------------------------------
# Far above every sleep in the supersede tests, so a second walk that starts
# can only have started because the parked one was woken.
_SUPERSEDE_RAW = {"response_timeout_ms": 5000, "screen_read_timeout_ms": 5000}


async def _assert_the_supersede_freed_the_next_walk(
    rig, start_first, *, expected_first_state,
):
    """Park a walk build, say 'show numbers' again, and sample the walks.

    ``start_first`` is an async callable that starts the build which will
    park: every walk request is held unanswered from here on, so the build
    that issued it stays parked on its Input round trip, the position a
    superseded build is in when the machine leaves it behind. Returns
    ``(walks_started, stale_send_answered)`` sampled 0.2 s after the
    re-said 'show numbers'.
    """

    rig.hold_walks = True
    await start_first()
    assert rig.machine.state is expected_first_state
    # Long enough for the first build to reach its send await and park.
    await asyncio.sleep(0.05)
    assert len(rig.held) == 1, (
        "test setup: the first build must be parked on its walk send before "
        f"the supersede; Logic had issued {len(rig.held)} walk send(s)"
    )

    await rig.controller.handle_overlay_command("show", "tr-show")
    # Long enough for a freed walk to start, far short of the 5 s deadline
    # the stalled send would otherwise have to reach.
    await asyncio.sleep(0.20)
    # The woken build cancels its own send, so "answered" means a reply was
    # set on the first send, not merely that the send ended.
    first = rig.held[0][1]
    return len(rig.held), first.done() and not first.cancelled()


def test_show_numbers_during_walk_in_flight_wakes_the_parked_walk_build():
    """'show numbers' spoken during a walk must not queue behind the old walk.

    wh-overlay-slow-uia-stale-badges.24, path 1 of 4: WALK_IN_FLIGHT
    SHOW_NUMBERS through ``_restart_walk``. The cell bumps the generation and
    dispatches a fresh build, which makes the parked one stale, but the
    ``_abort_inflight_overlay_build`` call in ``handle_overlay_command`` ran
    only when the machine had just left POST_CLICK_SETTLING. So the fresh walk
    queued behind the dead one on ``_overlay_effect_lock`` and did not start
    until ``response_timeout_ms`` expired -- a key ``ui/click_config.py``
    validates with a minimum of 100 and no ceiling.

    Same Logic-side proof, and the same limit on it, as the settle tests
    above: ``send_request`` is a mock, so this shows Logic ISSUES the second
    request instead of parking behind the dead one. Input's single synchronous
    command loop is wh-overlay-slow-uia-stale-badges.11 and is not this
    branch's work.
    """

    async def _run():
        async with _rig(_SUPERSEDE_RAW) as rig:
            return await _assert_the_supersede_freed_the_next_walk(
                rig, rig.show,
                expected_first_state=OverlayState.WALK_IN_FLIGHT,
            )

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled first walk must still be unanswered when the test "
        "samples; otherwise the second walk could have started simply "
        "because the first send finished"
    )
    assert walks_started == 2, (
        "a re-said 'show numbers' during walk_in_flight must wake the walk "
        "build it superseded so Logic can ISSUE its own start_overlay_walk; "
        f"Logic had issued {walks_started} walk send(s), so the fresh request "
        "was still queued behind the dead one inside Logic"
    )


def test_show_numbers_during_refresh_in_flight_wakes_the_parked_build():
    """'show numbers' during a refresh must not queue behind the old refresh.

    wh-overlay-slow-uia-stale-badges.24, path 3 of 4: REFRESH_IN_FLIGHT
    SHOW_NUMBERS through ``_refresh_supersede``. The user is looking at badges
    while a refresh walk runs, says 'show numbers' again, and the cell bumps
    the generation and dispatches a fresh build. The refresh build parked on
    its Input round trip is now stale and nothing woke it.
    """

    async def _run():
        async with _rig(_SUPERSEDE_RAW) as rig:
            await rig.paint("snap-24c")
            # A menu closes over the painted window: the refresh starts.
            return await _assert_the_supersede_freed_the_next_walk(
                rig, rig.menu_close,
                expected_first_state=OverlayState.REFRESH_IN_FLIGHT,
            )

    walks_started, stale_send_answered = asyncio.run(_run())

    assert not stale_send_answered, (
        "the stalled refresh walk must still be unanswered when the test "
        "samples; otherwise the second walk could have started simply "
        "because the first send finished"
    )
    assert walks_started == 2, (
        "a re-said 'show numbers' during refresh_in_flight must wake the "
        "refresh build it superseded so Logic can ISSUE its own "
        f"start_overlay_walk; Logic had issued {walks_started} walk send(s), "
        "so the fresh request was still queued behind the dead one inside "
        "Logic"
    )


# ---------------------------------------------------------------------------
# wh-overlay-timer-cancel-race: a timeout callback already ready in the loop
# must not act once a same-pair transition has committed.
#
# Four transitions keep the (overlay_session_id, paint_generation) pair the
# timer was armed for, so the machine's generation gate cannot reject the
# TIMEOUT. Each of them emits CANCEL_TIMER, but that effect is dispatched as a
# task and cannot run until a later loop turn -- and a callback already in the
# ready queue runs in this one. Measured on the machine before these tests were
# written: family 1 CLOSES a successful build; families 2, 3 and 4 reach the
# invalid cell and enter ERROR.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-rewalk-after-filter.1.3: the pending build target.
#
# ``_overlay_tracked_identity`` names the LATEST PIN, and the pin happens only
# after a build response comes back. A Pattern Manager tree change that
# arrives WHILE the build is in flight therefore had no truthful window to be
# judged against: on the first session of a run the field is still None, and
# on a cross-window refresh it still names the window that lost the focus.
# ``_overlay_dispatch_build`` now samples the foreground once, at the single
# build funnel, into ``_overlay_pending_build_identity``. Three paths end it:
# the pin consumes it, entry to closed clears it, and the next dispatch
# replaces it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# wh-overlay-rewalk-after-filter.1.4: a tree change queued ahead of the
# AUTO_OPEN effect task.
#
# ``_perform_auto_open_ambiguous`` commits closed -> walk_in_flight
# synchronously, but ``_perform_overlay_effects`` only SCHEDULES the effect
# batch, and the pending build target is sampled inside that batch. A
# Pattern Manager tree change already waiting on the loop runs in between.
# AUTO_OPEN repaints the snapshot the ambiguous click walked BEFORE the
# change, so a dropped event paints the pre-filter badges. The pair of tests
# below differs only in when the event is delivered.
# ---------------------------------------------------------------------------


_TREE_WINDOW = Window(
    hwnd=4242, pid=4321, process_name="python.exe",
    class_name="TkTopLevel", create_time_s=1_700_000_007.0,
)


def _replay_auto_open_with_a_tree_change(*, event_ahead_of_effects: bool):
    """Drive a first-session AUTO_OPEN with one Pattern Manager tree change.

    The spoken "click cancel" gets an ambiguous reply, so the overlay
    auto-opens on the finalists. ``event_ahead_of_effects`` True queues the
    tree change with ``loop.call_soon`` while the ambiguous reply is being
    handed back, so it runs after AUTO_OPEN commits walk_in_flight but before
    the scheduled effect task starts -- the position of a GUI listener
    callback that is already ready on the Logic loop. False delivers the same
    event from inside the show_numbered_overlay send, after the effect task
    has sampled the foreground window.

    Returns the ordered actions sent to Input, the ``snapshot_id`` of every
    ``paint_overlay`` put on the GUI queue, and the machine's final state.
    """

    result: dict[str, Any] = {}

    async def _run():
        async with _rig(windows=(_TREE_WINDOW,)) as rig:
            loop = asyncio.get_running_loop()

            def _deliver_tree_change():
                rig.overlay.on_pattern_manager_tree_change(4242, 1)

            def _click(params, _on_late):
                if event_ahead_of_effects:
                    loop.call_soon(_deliver_tree_change)
                return _ambiguous_response(
                    snapshot_id="pre-filter",
                    item_ids=("pre-filter-item-1", "pre-filter-item-2"),
                )

            def _show(params, _on_late):
                if not event_ahead_of_effects:
                    _deliver_tree_change()
                # AUTO_OPEN re-serves the snapshot the ambiguous click
                # walked, which predates the tree change.
                return _show_response(
                    "pre-filter", params.get("overlay_session_id", 0),
                    params.get("paint_generation", 0), 1, 2,
                )

            rig.answers["click_element"] = _click
            rig.answers["show_numbered_overlay"] = _show
            rig.walk_snapshots.append("post-filter")
            await rig.controller.forward_click_element(_amb_query(), "tr-1-4")
            await settle()
            paints = rig.payloads("paint_overlay")
            assert paints, f"no paint_overlay: {rig.outputs()}"
            await rig.ack(paints[-1])
            result["sent"] = [
                action for kind, action, _p in rig.log if kind == "input"
            ]
            result["paints"] = [
                p.get("snapshot_id") for p in rig.payloads("paint_overlay")
            ]
            result["state"] = rig.machine.state

    asyncio.run(_run())
    return result


def test_tree_change_queued_ahead_of_the_auto_open_build_is_not_dropped():
    """wh-overlay-rewalk-after-filter.1.4: an event queued before the build.

    Ordering pinned: the ambiguous click commits AUTO_OPEN (closed ->
    walk_in_flight) synchronously; a Pattern Manager tree change that was
    already ready on the Logic loop runs NEXT; only then does the scheduled
    effect task start and dispatch show_numbered_overlay. The event is for
    the foreground window, so it must be accepted: the build restarts as a
    fresh walk, and the badges that reach the screen come from the
    post-change tree, not from the snapshot the click walked before it.
    """
    result = _replay_auto_open_with_a_tree_change(event_ahead_of_effects=True)

    assert "start_overlay_walk" in result["sent"], (
        "tree change queued ahead of the AUTO_OPEN effect task was dropped: "
        f"no superseding walk was sent (sent={result['sent']})"
    )
    assert result["state"] is OverlayState.PAINTED
    assert result["paints"] and result["paints"][-1] == "post-filter", (
        "AUTO_OPEN painted the pre-filter snapshot after the tree change "
        f"(paints={result['paints']})"
    )


def test_tree_change_after_the_auto_open_build_started_is_accepted():
    """wh-overlay-rewalk-after-filter.1.4 control: the same event, delivered late.

    Ordering pinned: the effect task has already started and sampled the
    foreground window when the tree change arrives (inside the
    show_numbered_overlay send). Everything else matches the test above, so
    this passing while that one fails shows the only difference is the
    delivery order.
    """
    result = _replay_auto_open_with_a_tree_change(
        event_ahead_of_effects=False
    )

    assert "start_overlay_walk" in result["sent"], (
        f"no superseding walk was sent (sent={result['sent']})"
    )
    assert result["sent"].index("start_overlay_walk") > result["sent"].index(
        "show_numbered_overlay"
    )
    assert result["state"] is OverlayState.PAINTED
    assert result["paints"] and result["paints"][-1] == "post-filter", (
        f"paints={result['paints']}"
    )


# ---------------------------------------------------------------------------
# wh-numbers-stay-setting: [click] overlay_keep_numbers
#
# False (the default) copies Windows Voice Access: a successful click on a
# number removes the numbers, and a change to another application window
# removes them. True keeps the long-standing refresh. Every other trigger
# (a stale-position refusal, a menu, the browser timer, the Pattern Manager)
# refreshes under either value.
# ---------------------------------------------------------------------------

_NS_KEEP_OFF_RAW = {"enabled": True}
_NS_KEEP_FALSE_RAW = {"enabled": True, "overlay_keep_numbers": False}
_NS_KEEP_ON_RAW = {"enabled": True, "overlay_keep_numbers": True}


def _ns_machine(raw):
    from services.wheelhouse.numbered_overlay.helpers import make_click_overlay_state_machine
    from services.wheelhouse.ui.click_config import ClickConfig

    return make_click_overlay_state_machine(ClickConfig.from_raw(raw))


def _ns_click(
    raw, response, *, late=False, in_refresh=False, settle_refresh=False,
    flip_keep_to=None,
):
    """Run one spoken badge click; return (machine fields, outputs).

    The numbers are painted through 'show numbers' and their GUI ack, and
    every walk from then on is held unanswered, so a refresh the click starts
    stays in flight. ``in_refresh`` closes a menu over the window first, so
    the click is dispatched while that refresh is in flight at the click's
    own generation; ``settle_refresh`` then answers that refresh walk
    ("snap-ns-2") and acknowledges its paint, which is what consumes the
    deferred post-click replay. ``late`` lets the click send time out and
    delivers ``response`` as the late reply. ``flip_keep_to`` sets
    ``machine.keep_numbers`` after the click is sent and before its reply
    arrives, as a settings save made by that same click does. ``outputs``
    are the overlay outputs from the click on.
    """

    async def _run():
        async with _rig(raw) as rig:
            await rig.paint("snap-ns", (1, 2, 3))
            rig.hold_walks = True
            if in_refresh:
                await rig.menu_close()
                assert rig.machine.state is OverlayState.REFRESH_IN_FLIGHT
            late_reply: dict = {}

            def _flip():
                if flip_keep_to is not None:
                    rig.machine.keep_numbers = flip_keep_to

            def _click(params, on_late_response):
                if late:
                    late_reply["deliver"] = on_late_response
                    raise asyncio.TimeoutError()
                _flip()
                return response

            rig.answers["click_snapshot_item"] = _click
            mark = rig.mark()
            await rig.say_click(2, "tr-ns")
            if late:
                _flip()
                late_reply["deliver"](response)
                await settle()
            if settle_refresh:
                params = rig.held[0][0]
                rig.release(0, rig.walk_reply(params, "snap-ns-2", (1, 2, 3)))
                await settle()
                paints = [
                    p for p in rig.payloads("paint_overlay", mark)
                    if p.get("snapshot_id") == "snap-ns-2"
                ]
                if paints:
                    await rig.ack(paints[-1])
            return rig.snapshot(), rig.outputs(mark)

    return asyncio.run(_run())


def _ns_ok():
    return _click_response("ok", trace_id="tr-ns")


def _ns_refused():
    return _click_response(
        "execution_failed", reason="bounds_stale", trace_id="tr-ns",
    )


def _assert_numbers_removed(machine, outputs):
    assert machine.state is OverlayState.CLOSED
    assert machine.pinned_snapshot_id is None
    assert ("gui", "clear_overlay") in outputs
    assert ("input", "start_overlay_walk") not in outputs


def _assert_numbers_refreshed(machine, outputs):
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert ("input", "start_overlay_walk") in outputs


def test_overlay_keep_numbers_key_absent_removes_numbers_after_a_click():
    machine, outputs = _ns_click(_NS_KEEP_OFF_RAW, _ns_ok())
    _assert_numbers_removed(machine, outputs)


def test_a_save_made_by_the_click_does_not_change_that_click_off_to_on():
    """Acceptance item 4: the value applies from the NEXT click after the save.

    A click on the settings check box saves the new value, and the save can
    reach Logic before the click's own reply. That click still follows the
    value it was sent under (off), so the numbers close.
    """
    machine, outputs = _ns_click(_NS_KEEP_OFF_RAW, _ns_ok(), flip_keep_to=True)
    assert machine.keep_numbers is True
    _assert_numbers_removed(machine, outputs)


def test_a_save_made_by_the_click_does_not_change_that_click_on_to_off():
    machine, outputs = _ns_click(_NS_KEEP_ON_RAW, _ns_ok(), flip_keep_to=False)
    assert machine.keep_numbers is False
    _assert_numbers_refreshed(machine, outputs)


def test_a_save_made_by_the_click_does_not_change_that_late_click_off_to_on():
    machine, outputs = _ns_click(
        _NS_KEEP_OFF_RAW, _ns_ok(), late=True, flip_keep_to=True,
    )
    _assert_numbers_removed(machine, outputs)


def test_a_save_made_by_the_click_does_not_change_that_late_click_on_to_off():
    machine, outputs = _ns_click(
        _NS_KEEP_ON_RAW, _ns_ok(), late=True, flip_keep_to=False,
    )
    _assert_numbers_refreshed(machine, outputs)


def test_overlay_keep_numbers_false_late_success_removes_numbers():
    machine, outputs = _ns_click(_NS_KEEP_OFF_RAW, _ns_ok(), late=True)
    _assert_numbers_removed(machine, outputs)


def test_overlay_keep_numbers_true_late_success_shows_new_numbers():
    machine, outputs = _ns_click(_NS_KEEP_ON_RAW, _ns_ok(), late=True)
    _assert_numbers_refreshed(machine, outputs)


def test_overlay_keep_numbers_false_late_stale_refusal_shows_new_numbers():
    machine, outputs = _ns_click(_NS_KEEP_OFF_RAW, _ns_refused(), late=True)
    _assert_numbers_refreshed(machine, outputs)


def test_overlay_keep_numbers_false_success_during_a_refresh_closes_at_the_reply_not_after_it_settles():
    """A success that arrives while a refresh is in flight removes the numbers.

    The numbers close at once when the reply arrives, so the refresh that was
    in flight has nothing left to paint: its build response and paint ack are
    stale events for a closed session.
    """
    machine, outputs = _ns_click(
        _NS_KEEP_OFF_RAW, _ns_ok(), in_refresh=True, settle_refresh=True,
    )
    assert machine.state is OverlayState.CLOSED
    assert machine.pinned_snapshot_id is None


def test_overlay_keep_numbers_false_deferred_refusal_replay_shows_new_numbers():
    machine, outputs = _ns_click(
        _NS_KEEP_OFF_RAW, _ns_refused(), in_refresh=True, settle_refresh=True,
    )
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT


def test_overlay_keep_numbers_true_deferred_success_replay_shows_new_numbers():
    machine, outputs = _ns_click(
        _NS_KEEP_ON_RAW, _ns_ok(), in_refresh=True, settle_refresh=True,
    )
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT


def test_overlay_keep_numbers_false_wins_over_settle_after_click_end_to_end():
    raw = {
        "enabled": True,
        "overlay_settle_after_click": True,
        "overlay_keep_numbers": False,
    }
    machine, outputs = _ns_click(raw, _ns_ok())
    _assert_numbers_removed(machine, outputs)


# -- a refresh that starts between the click and its reply -------------------
#
# A same-session refresh (a menu closing, the browser timer, an owned dialog)
# can start AFTER "click N" is sent and BEFORE the success reply arrives. The
# refresh bumps the generation, so the reply no longer matches the pair the
# click was dispatched at. A successful click with ``overlay_keep_numbers``
# false must still remove the numbers (item 2a), under every one of those
# interleaves and whatever ``overlay_settle_after_click`` says.


def _ns_click_with_interleave(
    raw, interleave, response, *, windows=(_RIG_WINDOW,), offset_clock=False,
):
    """Say "click 4"; run ``interleave`` while the reply is outstanding.

    The numbers are painted through 'show numbers' and their GUI ack, and
    every walk from then on is held unanswered. ``interleave(rig)`` is an
    async callable that runs inside the click send, before ``response`` is
    returned. Returns ``(machine fields, outputs)``, the outputs taken from
    the click on.
    """

    async def _run():
        async with _rig(raw, windows=windows, offset_clock=offset_clock) as rig:
            await rig.paint("snap-ns", (1, 2, 3, 4, 5))
            rig.hold_walks = True

            async def _click(params, _on_late):
                await interleave(rig)
                return response

            rig.answers["click_snapshot_item"] = _click
            mark = rig.mark()
            await rig.say_click(4, "tr-ns")
            return rig.snapshot(), rig.outputs(mark)

    return asyncio.run(_run())


def _assert_numbers_closed_after_race(machine, outputs):
    """Closed, cleared and unpinned; no build starts after the clear.

    The refresh that started before the reply dispatched its own build, so
    ``start_overlay_walk`` appears earlier in the list; the success must end
    it.
    """
    assert machine.state is OverlayState.CLOSED
    assert machine.pinned_snapshot_id is None
    clear_at = outputs.index(("gui", "clear_overlay"))
    assert ("input", "start_overlay_walk") not in outputs[clear_at:]
    assert ("input", "unpin_snapshot") in outputs[clear_at:]


async def _menu_close(rig):
    await rig.menu_close()
    assert rig.machine.state is OverlayState.REFRESH_IN_FLIGHT


async def _browser_timer_refresh(rig):
    # The first keepalive tick comes 15 s after the paint; the paint is then
    # older than the 1 s browser window.
    await rig.clock.advance(16.0)
    assert rig.machine.state is OverlayState.REFRESH_IN_FLIGHT


_NS_BROWSER_WINDOW = Window(
    hwnd=0x4C01, pid=4301, process_name="chrome.exe",
    class_name="Chrome_WidgetWin_1", create_time_s=1_700_000_200.0,
)


def test_refresh_starting_before_the_click_reply_still_closes_the_numbers():
    raw = dict(
        _NS_KEEP_OFF_RAW, overlay_browser_refresh_seconds=1,
        browser_processes=["chrome.exe"], browser_processes_extend=[],
    )
    machine, outputs = _ns_click_with_interleave(
        raw, _browser_timer_refresh, _ns_ok(),
        windows=(_NS_BROWSER_WINDOW,), offset_clock=True,
    )
    assert outputs.index(("input", "start_overlay_walk")) < outputs.index(
        ("gui", "clear_overlay")
    ), "the browser-timer refresh did not start before the reply"
    _assert_numbers_closed_after_race(machine, outputs)


def test_refresh_before_the_click_reply_keeps_new_numbers_with_keep_numbers_true():
    machine, outputs = _ns_click_with_interleave(
        _NS_KEEP_ON_RAW, _menu_close, _ns_ok(),
    )
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert ("gui", "clear_overlay") not in outputs


def test_refresh_before_a_stale_position_refusal_keeps_new_numbers():
    machine, outputs = _ns_click_with_interleave(
        _NS_KEEP_OFF_RAW, _menu_close, _ns_refused(),
    )
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert ("gui", "clear_overlay") not in outputs


def test_a_success_for_another_overlay_session_does_not_close_the_numbers():
    """A reply for a click dispatched against a different overlay session
    keeps today's behaviour: the session the click belonged to is gone, so the
    reply does nothing to the overlay that is on the screen now.

    The click "click 2" goes out in the first session and its send times
    out. "hide numbers" ends that session, and an ambiguous "click cancel"
    auto-opens a second session (no 'show numbers', so the reply is not
    excused by a re-said 'show numbers'). Then the success for the first
    session's click arrives late.
    """

    async def _run():
        async with _rig(_NS_KEEP_OFF_RAW) as rig:
            await rig.paint("snap-ns", (1, 2, 3))
            first_sid = rig.machine.overlay_session_id
            late_reply: dict = {}

            def _click(params, on_late_response):
                late_reply["deliver"] = on_late_response
                raise asyncio.TimeoutError()

            def _show(params, _on_late):
                return _show_response(
                    "snap-amb", params.get("overlay_session_id", 0),
                    params.get("paint_generation", 0), 1, 2,
                )

            rig.answers["click_snapshot_item"] = _click
            rig.answers["click_element"] = (
                lambda params, _late: _ambiguous_response()
            )
            rig.answers["show_numbered_overlay"] = _show
            await rig.say_click(2, "tr-ns")
            await rig.controller.handle_overlay_command("hide", "tr-hide")
            await settle()
            assert rig.machine.state is OverlayState.CLOSED
            await rig.controller.forward_click_element(_amb_query(), "tr-amb")
            await settle()
            paints = rig.payloads("paint_overlay")
            await rig.ack(paints[-1])
            assert rig.machine.state is OverlayState.PAINTED
            assert rig.machine.overlay_session_id != first_sid
            mark = rig.mark()
            late_reply["deliver"](_ns_ok())
            await settle()
            return rig.snapshot(), rig.outputs(mark)

    machine, outputs = asyncio.run(_run())
    assert machine.state is OverlayState.PAINTED
    assert ("gui", "clear_overlay") not in outputs


# -- a re-said "show numbers" after the click was sent (decision D5) -------


def _ns_click_then_show_numbers(raw, *, late):
    """Say "click 4", then "show numbers", then deliver the success.

    ``late``: the click times out, the user says "show numbers", and the
    success arrives late. Otherwise "show numbers" is said while the click
    request is outstanding and the success arrives on time; the public
    ``grid_pointer_channel_quiescent`` then answers True at once, because
    the real one waits for the outstanding click itself.
    Returns ``(machine fields, outputs)``, the outputs taken from the click
    on.
    """

    async def _run():
        async with _rig(raw) as rig:
            await rig.paint("snap-ns", (1, 2, 3, 4, 5))
            rig.hold_walks = True
            late_reply: dict = {}

            async def _click(params, on_late_response):
                late_reply["deliver"] = on_late_response
                if late:
                    raise asyncio.TimeoutError()
                await rig.controller.handle_overlay_command("show", "tr-show")
                return _ns_ok()

            async def _quiescent(*, trace_id):
                return True

            rig.answers["click_snapshot_item"] = _click
            if not late:
                rig.controller.grid_pointer_channel_quiescent = _quiescent  # type: ignore[method-assign]
            mark = rig.mark()
            await rig.say_click(4, "tr-ns")
            if late:
                await rig.controller.handle_overlay_command("show", "tr-show")
                await settle()
                late_reply["deliver"](_ns_ok())
                await settle()
            return rig.snapshot(), rig.outputs(mark)

    return asyncio.run(_run())


def test_a_late_success_after_a_re_said_show_numbers_keeps_the_numbers():
    """The user asked for numbers AFTER the click: the late success must not
    remove them. The reply takes today's path."""
    machine, outputs = _ns_click_then_show_numbers(_NS_KEEP_OFF_RAW, late=True)
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert ("gui", "clear_overlay") not in outputs


def test_an_on_time_success_after_a_re_said_show_numbers_keeps_the_numbers():
    machine, outputs = _ns_click_then_show_numbers(_NS_KEEP_OFF_RAW, late=False)
    assert machine.state is OverlayState.REFRESH_IN_FLIGHT
    assert ("gui", "clear_overlay") not in outputs


# -- the window facts seam ----------------------------------------------------


def test_overlay_window_facts_reads_the_top_level_window_not_the_owner(monkeypatch):
    """G3: a dialog the numbered window owns has GA_ROOT = itself and
    GA_ROOTOWNER = its owner. The facts must carry GA_ROOT."""
    import sys
    import types

    import services.wheelhouse.numbered_overlay.helpers as helpers_module

    GA_ROOT, GA_ROOTOWNER = 2, 3
    dialog, owner = 900, 500
    fake_con = types.SimpleNamespace(GA_ROOT=GA_ROOT, GA_ROOTOWNER=GA_ROOTOWNER)
    fake_gui = types.SimpleNamespace(
        IsWindow=lambda h: True,
        GetClassName=lambda h: "#32770",
        GetAncestor=lambda h, flag: h if flag == GA_ROOT else owner,
    )
    fake_process = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda h: (7, 4321),
    )
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32process", fake_process)

    facts = helpers_module._overlay_window_facts(dialog)

    assert facts is not None
    assert facts.root == dialog
    assert facts.pid == 4321
    assert facts.class_name == "#32770"


# -- foreground changes -------------------------------------------------------


# -- the other refresh triggers keep today's behaviour -----------------------


# -- the settle refire --------------------------------------------------------


# -- applying a saved value with no restart ----------------------------------


class _FakeConfigService:
    def __init__(self, click):
        self.click = click
        self.reads: list[str] = []

    def get(self, key, default=None):
        self.reads.append(key)
        if key == "click":
            return self.click
        return default


def _ns_save_controller(*, saved=True, raw=None):
    machine = _ns_machine(raw if raw is not None else _NS_KEEP_OFF_RAW)
    controller = _controller(machine=machine)
    controller.config_service = _FakeConfigService({"enabled": True})
    controller.state_manager = MagicMock()
    calls: list = []

    async def _set_config_value(key, value, request_id=None):
        calls.append(("one", key, value, request_id))
        controller.config_service.click[key.split(".", 1)[1]] = value
        return saved

    async def _set_config_values(values, request_id=None):
        calls.append(("many", dict(values), request_id))
        for key, value in values.items():
            if key.startswith("click."):
                controller.config_service.click[key.split(".", 1)[1]] = value
        return saved

    controller.state_manager.set_config_value = _set_config_value
    controller.state_manager.set_config_values = _set_config_values
    return controller, machine, calls


def test_saved_keep_numbers_value_reaches_the_machine_without_a_restart():
    controller, machine, calls = _ns_save_controller()
    assert machine.keep_numbers is False

    result = asyncio.run(
        controller._set_config_value_and_apply(
            "click.overlay_keep_numbers", True, request_id="r1",
        )
    )
    assert result is True
    assert machine.keep_numbers is True
    assert calls == [("one", "click.overlay_keep_numbers", True, "r1")]

    asyncio.run(
        controller._set_config_value_and_apply(
            "click.overlay_keep_numbers", False, request_id="r2",
        )
    )
    assert machine.keep_numbers is False


def test_saved_keep_numbers_value_in_a_group_save_reaches_the_machine():
    controller, machine, calls = _ns_save_controller()

    result = asyncio.run(
        controller._set_config_values_and_apply(
            {"click.overlay_keep_numbers": True, "OTHER": 1}, request_id="g1",
        )
    )

    assert result is True
    assert machine.keep_numbers is True
    assert calls[0][0] == "many"


def test_failed_save_leaves_the_machine_unchanged_and_returns_false():
    controller, machine, _calls = _ns_save_controller(saved=False)

    result = asyncio.run(
        controller._set_config_value_and_apply(
            "click.overlay_keep_numbers", True, request_id="r1",
        )
    )

    assert result is False
    assert machine.keep_numbers is False


def test_saving_an_unrelated_key_does_not_touch_the_machine():
    controller, machine, _calls = _ns_save_controller(raw=_NS_KEEP_ON_RAW)
    controller.config_service.click["x"] = 0
    assert machine.keep_numbers is True

    asyncio.run(controller._set_config_value_and_apply("click.x", 1))
    asyncio.run(controller._set_config_values_and_apply({"STT": 2}))

    assert machine.keep_numbers is True
    assert controller.config_service.reads == []


def test_gui_handler_map_routes_both_save_commands_through_the_live_apply():
    controller = _controller()
    seen: list = []

    async def _direct_one(key, value, request_id=None):
        seen.append(("state_manager one", key, value, request_id))
        return True

    async def _direct_many(values, request_id=None):
        seen.append(("state_manager many", values, request_id))
        return True

    # A handler that skips the live apply lands here and records a different
    # entry; a MagicMock would raise before the assertion could fail.
    controller.state_manager = MagicMock()
    controller.state_manager.set_config_value = _direct_one
    controller.state_manager.set_config_values = _direct_many

    async def _one(key, value, request_id=None):
        seen.append(("one", key, value, request_id))
        return True

    async def _many(values, request_id=None):
        seen.append(("many", values, request_id))
        return True

    controller._set_config_value_and_apply = _one  # type: ignore[method-assign]
    controller._set_config_values_and_apply = _many  # type: ignore[method-assign]

    def _run(command):
        handlers = controller._build_gui_handler_map(command)
        captured: list = []
        controller.create_task_with_error_handling = (  # type: ignore[method-assign]
            lambda coro, name: captured.append(coro)
        )
        handlers[command["action"]]()
        for coro in captured:
            asyncio.run(coro)

    _run({
        "action": "set_config_value", "key": "click.overlay_keep_numbers",
        "value": True, "request_id": "r1",
    })
    _run({
        "action": "set_config_values",
        "values": {"click.overlay_keep_numbers": False}, "request_id": "r2",
    })

    assert seen == [
        ("one", "click.overlay_keep_numbers", True, "r1"),
        ("many", {"click.overlay_keep_numbers": False}, "r2"),
    ]


# -- gaps the mutation gate found (wh-numbers-stay-setting) ------------------


def test_failed_group_save_returns_false_and_leaves_the_machine_unchanged():
    controller, machine, _calls = _ns_save_controller(saved=False)

    result = asyncio.run(
        controller._set_config_values_and_apply(
            {"click.overlay_keep_numbers": True}, request_id="g1",
        )
    )

    assert result is False
    assert machine.keep_numbers is False


# ---------------------------------------------------------------------------
# wh-overlay-refresh-after-mouse: the mouse refresh, in the states a process
# boundary cannot reach (paused, an automatic-hide refresh) and the rule a
# boundary cannot see (an injected click while a number click is in flight).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _mouse_refresh_controller(raw: Optional[dict] = None):
    """A ``_rig`` on the fake clock, its numbers painted ("snap-m1").

    Yields ``(rig, pair)``, ``pair`` being the painted (session, generation).
    A click or wheel step reaches the overlay through its hook entry,
    ``rig.overlay.note_pointer_input``, which posts to the Logic loop; the
    quiet time is 500 ms of ``rig.clock``.
    """

    async with _rig(raw, offset_clock=True) as rig:
        pair = await rig.paint("snap-m1")
        yield rig, pair


def _mouse_settled_lines(records) -> list:
    """The log lines of the mouse refresh's timer, as (level, message)."""

    return [
        (r.levelno, r.getMessage()) for r in records
        if "mouse input settled" in r.getMessage()
        or "reading the numbers again" in r.getMessage()
    ]


@pytest.mark.parametrize(
    "state, auto_hide",
    [
        pytest.param(OverlayState.PAUSED, False, id="paused"),
        pytest.param(OverlayState.REFRESH_IN_FLIGHT, True, id="auto-hide-refresh"),
        pytest.param(OverlayState.POST_CLICK_SETTLING, False, id="settling"),
        pytest.param(OverlayState.ERROR, False, id="error"),
        pytest.param(OverlayState.CLOSED, False, id="closed"),
    ],
)
def test_mouse_refresh_settled_while_numbers_hidden_does_nothing(
    state, auto_hide, caplog,
):
    """RW-38: the quiet time ends while no numbers show: no refresh, no re-arm.

    A wheel step over painted numbers starts the quiet time; the machine is
    then put in ``state`` (paused and an automatic-hide refresh are out of a
    process boundary's reach) before the quiet time ends.
    """

    async def _run():
        async with _mouse_refresh_controller() as (rig, _pair):
            rig.overlay.note_pointer_input(False, False)
            await settle()
            rig.machine.state = state
            rig.machine.auto_hide_in_flight = auto_hide
            mark = rig.mark()
            caplog.clear()
            with caplog.at_level(logging.DEBUG), caplog.at_level(
                logging.DEBUG,
                logger="services.wheelhouse.numbered_overlay.controller",
            ):
                await rig.clock.advance(0.6)
                lines = _mouse_settled_lines(caplog.records)
            hidden = rig.outputs(mark)
            # No re-arm: back in painted, one more quiet time passes and
            # nothing goes out.
            rig.machine.state = OverlayState.PAINTED
            rig.machine.auto_hide_in_flight = False
            await rig.clock.advance(0.6)
            rearmed = rig.outputs(mark)
            # Control: a new wheel step over the painted numbers refreshes.
            rig.overlay.note_pointer_input(False, False)
            await settle()
            await rig.clock.advance(0.6)
            return lines, hidden, rearmed, rig.outputs(mark)

    lines, hidden, rearmed, control = asyncio.run(_run())
    assert lines == []
    assert hidden == []
    assert rearmed == []
    assert control.count(("input", "start_overlay_walk")) == 1


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(OverlayState.PAINTED, id="painted"),
        pytest.param(OverlayState.REFRESH_IN_FLIGHT, id="refresh"),
    ],
)
def test_mouse_refresh_settled_while_numbers_show_applies_one_focus_change(
    state, caplog,
):
    """RW-37 control: numbers show, so one FOCUS_CHANGE and one DEBUG line.

    The FOCUS_CHANGE is seen as its output: one start_overlay_walk at the
    next generation, with no clear_overlay (the old badges stay), and the
    apply's log line names the source "mouse input settled". The refresh
    case is a menu-close refresh still in flight; the mouse refresh
    supersedes it.
    """

    async def _run():
        async with _mouse_refresh_controller() as (rig, pair):
            rig.hold_walks = True
            if state is OverlayState.REFRESH_IN_FLIGHT:
                await rig.menu_close()
                assert rig.machine.state is OverlayState.REFRESH_IN_FLIGHT
            mark = rig.mark()
            rig.overlay.note_pointer_input(False, False)
            await settle()
            caplog.clear()
            with caplog.at_level(logging.DEBUG), caplog.at_level(
                logging.DEBUG,
                logger="services.wheelhouse.numbered_overlay.controller",
            ):
                await rig.clock.advance(0.6)
                lines = _mouse_settled_lines(caplog.records)
            walks = [
                (p["overlay_session_id"], p["paint_generation"])
                for p in rig.payloads("start_overlay_walk", mark)
            ]
            return pair, walks, rig.outputs(mark), lines

    pair, walks, outputs, lines = asyncio.run(_run())
    step = 2 if state is OverlayState.REFRESH_IN_FLIGHT else 1
    assert walks == [(pair[0], pair[1] + step)]
    assert ("gui", "clear_overlay") not in outputs
    assert [level for level, message in lines
            if "reading the numbers again" in message] == [logging.DEBUG]
    applied = [message for _level, message in lines
               if "mouse input settled" in message]
    assert len(applied) == 1
    assert "mouse input settled -> applied focus_change" in applied[0]


@pytest.mark.parametrize(
    "click, injected, arms",
    [
        pytest.param(True, True, False, id="injected-click"),
        pytest.param(True, False, True, id="user-click"),
        pytest.param(False, True, True, id="injected-wheel"),
    ],
)
def test_mouse_input_while_a_number_click_is_in_flight(click, injected, arms):
    """RW-39: an injected click during a number click arms no refresh.

    The user's own click and every wheel event still restart the quiet time
    (RW-37: WheelHouse scroll commands send injected wheel events). The
    input arrives while the "click two" request to Input is unanswered, and
    the quiet time passes before the reply; an armed quiet time shows as a
    start_overlay_walk sent before the reply.
    """

    async def _run():
        async with _mouse_refresh_controller() as (rig, _pair):
            rig.hold_walks = True
            seen: dict = {}

            async def _click(params, _on_late):
                mark = rig.mark()
                rig.overlay.note_pointer_input(click, injected)
                await settle()
                await rig.clock.advance(0.6)
                seen["outputs"] = rig.outputs(mark)
                return _click_response(
                    "ok", trace_id=params.get("trace_id", "tr"),
                )

            rig.answers["click_snapshot_item"] = _click
            await rig.say_click(2, "tr-mouse")
            return seen["outputs"]

    outputs = asyncio.run(_run())
    assert (("input", "start_overlay_walk") in outputs) is arms


def test_shutdown_stops_the_mouse_refresh():
    """wh-overlay-refresh-after-mouse.1.1: overlay.shutdown() cancels both
    mouse-refresh timers and stops the hook entry.

    The click on the tray Exit item arms the 500 ms quiet time. Without the
    cancel, the timer fires during the shutdown awaits on the still-live
    loop and sends a start_overlay_walk while the processes stop. Until
    stop_listeners runs, the pynput hook still delivers clicks, so the hook
    entry must post nothing after shutdown.

    The two timers are the quiet window a number click opens when it ends
    and the quiet time of the user's click after it: the two 500 ms timers
    the overlay arms on the Logic loop.
    """

    async def _run():
        async with _mouse_refresh_controller(_NS_KEEP_ON_RAW) as (rig, _pair):
            loop = asyncio.get_running_loop()
            rig.hold_walks = True
            armed: list = []
            real_call_later = loop.call_later

            def _call_later(delay, callback, *args, **kwargs):
                handle = real_call_later(delay, callback, *args, **kwargs)
                armed.append((delay, callback, handle))
                return handle

            with patch.object(loop, "call_later", new=_call_later):
                await rig.say_click(2, "tr-exit")
                rig.overlay.note_pointer_input(True, False)
                await settle()
            quiet_timers = [
                handle for delay, callback, handle in armed
                if delay == 0.5
                and getattr(callback, "__self__", None) is rig.overlay
            ]
            before = [h.cancelled() for h in quiet_timers]

            rig.overlay.shutdown()

            after = [h.cancelled() for h in quiet_timers]
            mark = rig.mark()
            await rig.clock.advance(0.6)
            timer_outputs = rig.outputs(mark)
            with patch.object(
                loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe,
            ) as posted:
                rig.overlay.note_pointer_input(True, False)
            post_calls = posted.call_count
            await settle()
            await rig.clock.advance(0.6)
            return before, after, timer_outputs, post_calls, rig.outputs(mark)

    before, after, timer_outputs, post_calls, outputs = asyncio.run(_run())
    assert before == [False, False]
    assert after == [True, True]
    assert timer_outputs == []
    assert post_calls == 0
    assert outputs == []
