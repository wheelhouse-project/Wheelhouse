"""A new grid / numbered-overlay session waits out stale pointer actions.

wh-mouse-grid.1.29: grid clicks, moves, and drags are dispatched as
background tasks (``_dispatch_mouse_action``), and nothing stopped a
later "show grid" / "grid next screen" / "show numbers" from painting a
NEW session while an earlier pointer action was still queued or running
in the Input process. The stale action then executed at the prior cell
coordinates under the fresh visual state -- worst for a multi-second
drag pressing and moving while the new grid is visible.

The fence under test: before a session-visibility change, Logic awaits
every outstanding pointer-action task and -- when the channel is suspect
after a timeout -- confirms it with the channel probe (Input's command
loop is single-threaded and in-order, so any probe reply proves earlier
payloads can no longer act). An unconfirmed channel withholds the new
session with a ``grid_pointer_pending`` notice, the same policy the next
pointer action follows.

Fixture pattern follows test_grid_speech_routing._make_controller: real
methods bound onto a MagicMock(spec=LogicController), real state
machines, a scripted ``app.send_request`` -- here extended so any mouse
action can be BLOCKED on an asyncio.Event or made to raise.

wh-mouse-grid.1.30 (round 14) widened the fence's inputs: by-name
``click_element`` and numbered-badge ``click_snapshot_item`` requests
are pointer-capable too (non-default gestures and the coordinate
fallback press real mouse buttons), so their timeouts mark the channel
suspect and the badge-click background task is registered with the
fence. wh-mouse-grid.1.31 (round 14, test-strength) added the
queued-before-lock tests: they run the session change INLINE with no
yield after the dispatch, so the fence executes before the dispatched
task's first step -- the one window where only the pending-task
registration (not the action lock) makes the fence wait.
"""
import sys
from pathlib import Path

test_file = Path(__file__).resolve()
project_root = test_file.parent.parent.parent.parent
wheelhouse_dir = test_file.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(wheelhouse_dir))

import asyncio
from unittest.mock import MagicMock

from services.wheelhouse.click_overlay_state import (
    ClickOverlayStateMachine,
    OverlayState,
)
from services.wheelhouse.click_snapshot_summary_cache import (
    ClickSnapshotSummaryCache,
)
from services.wheelhouse.grid_overlay_state import (
    GridEvent,
    GridEventKind,
    GridOverlayStateMachine,
    GridRect,
)

MON = GridRect(0, 0, 1920, 1080)


def _probe_reply(params) -> dict:
    """The reply UIActionHandler.channel_probe actually emits."""
    assert isinstance(params, dict) and params.get("trace_id"), (
        f"channel_probe request must carry a nonempty trace_id, got {params!r}"
    )
    return {
        "status": "ok",
        "action": "channel_probe",
        "trace_id": params["trace_id"],
        "last_mouse_action": None,
        "request_id": "req-probe",
    }


def _make_controller(*, grid_open: bool = True):
    from main import LogicController
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    for name in (
        "forward_click_element",
        "handle_grid_command",
        "handle_overlay_command",
        "_perform_grid_effects",
        "_forward_click_notice",
        "_close_grid_for_numbered_overlay",
        "_send_mouse_action",
        "_confirm_grid_mouse_channel",
        "_dispatch_mouse_action",
        "_dispatch_snapshot_item_click",
        "_send_snapshot_item_click",
        # wh-overlay-slow-uia-stale-badges.8: the send body moved into the
        # inner method (the outer wrapper clears the in-flight marker in a
        # finally); without binding it the spec'd mock swallows the send.
        "_send_snapshot_item_click_inner",
        "_cancel_held_click_after_success",
        # wh-mouse-grid.1.30: the shared registration helper; skipped
        # when it does not exist yet (pre-fix run fails on behavior).
        "_track_pending_pointer_task",
        "_grid_monitor_context_off_loop",
        "_grid_gesture_buttons",
        "_put_grid_gui_event",
        # The fence helper under test; skipped when it does not exist yet
        # so the pre-fix run fails on BEHAVIOR, not on fixture setup.
        "_grid_pointer_channel_quiescent",
    ):
        method = getattr(LogicController, name, None)
        if method is not None:
            setattr(c, name, method.__get__(c))

    c.click_config = ClickConfig.from_raw(
        {"enabled": True, "response_timeout_ms": 3000}
    )
    c._click_disabled_notice_shown = False

    grid = GridOverlayStateMachine()
    if grid_open:
        result = grid.apply(
            GridEvent(
                GridEventKind.OPEN_GRID,
                monitors=(MON,),
                focused_monitor=MON,
            )
        )
        assert grid.is_open, result
    c.grid_overlay_state = grid

    c.click_overlay_state = ClickOverlayStateMachine()
    c._overlay_focus_debouncer = FocusChangeDebouncer()
    c.click_overlay_state.state = OverlayState.CLOSED
    c.click_snapshot_summary_cache = ClickSnapshotSummaryCache()

    gui_events: list = []
    c.state_manager = MagicMock()
    c.state_manager.state_to_gui_queue.put_nowait = gui_events.append
    c._gui_events = gui_events

    # Per-action send behavior: an asyncio.Event blocks the reply until
    # set; a BaseException instance is raised; absent -> immediate ok.
    c._behavior = {}
    c._started = {}
    captured: dict = {"calls": []}

    async def _send_request(action, params=None, timeout_s=None,
                            on_late_response=None):
        captured["calls"].append((action, params, timeout_s))
        behavior = c._behavior.get(action)
        if action in ("click_point", "move_pointer", "perform_drag"):
            c._started[action] = True
            if isinstance(behavior, asyncio.Event):
                await behavior.wait()
            elif isinstance(behavior, BaseException):
                raise behavior
            from shared.mouse_action import MouseActionResponse

            return MouseActionResponse(
                status="ok", outcome="ok", reason=None,
                trace_id=(params or {}).get("trace_id", "t"),
            ).to_dict()
        if action == "channel_probe":
            if isinstance(behavior, BaseException):
                raise behavior
            return _probe_reply(params)
        if action == "click_snapshot_item":
            c._started[action] = True
            if isinstance(behavior, asyncio.Event):
                await behavior.wait()
            elif isinstance(behavior, BaseException):
                raise behavior
            from shared.click_element import ClickElementResponse

            return ClickElementResponse(
                status="ok", outcome="ok", reason=None,
                matched_names=(), snapshot_id=(params or {}).get("snapshot_id"),
                snapshot_summary=None, matched_name="item",
                trace_id=(params or {}).get("trace_id", "t"),
            ).to_dict()
        # click_element (by-name) and any overlay-build action: scripted
        # the same way; the default reply is an AMBIGUOUS by-name result
        # so the auto-open gate is reachable.
        if isinstance(behavior, asyncio.Event):
            await behavior.wait()
        elif isinstance(behavior, BaseException):
            raise behavior
        from shared.click_element import ClickElementResponse

        return ClickElementResponse(
            status="ok", outcome="ambiguous", reason=None,
            matched_names=("save", "save as"), snapshot_id="snap-1",
            snapshot_summary=None, matched_name=None, trace_id="trace",
            ambiguous_item_ids=("i1", "i2"),
        ).to_dict()

    c.app = MagicMock()
    c.app.send_request = _send_request
    c._captured = captured

    async def _hint(_trace):
        return None

    c._maybe_show_first_use_hint = _hint
    c._grid_monitor_context = lambda: ((MON,), MON)

    c._tasks = []

    def _create_task(coro, name=None):
        task = asyncio.get_event_loop().create_task(coro)
        c._tasks.append(task)
        return task

    c.create_task_with_error_handling = _create_task
    return c


async def _drain(c):
    while c._tasks:
        pending, c._tasks = c._tasks, []
        await asyncio.gather(*pending)


async def _wait_started(c, action: str):
    for _ in range(50):
        if c._started.get(action):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{action} send never started")


def _gui(c, action: str) -> list:
    return [e for e in c._gui_events if e.get("action") == action]


def _notices(c) -> list:
    return [e for e in c._gui_events if e.get("action") == "show_click_notice"]


def _reasons(c) -> list:
    return [n["reason"] for n in _notices(c)]


# ---------------------------------------------------------------------------
# 1. The reopen fence: one test per dispatched pointer action.
# ---------------------------------------------------------------------------


def _assert_reopen_waits(c, release: asyncio.Event, blocked_action: str):
    """Shared body: 'open' must not paint until the action resolves.

    Counts paint_grid events from a baseline because 'mark' repaints the
    grid (pin paint) before the drag scenario ever reopens.
    """

    async def scenario():
        baseline = len(_gui(c, "paint_grid"))
        open_task = asyncio.create_task(
            c.handle_grid_command("open", "tr-open", spoken="show grid")
        )
        await asyncio.sleep(0.05)
        assert len(_gui(c, "paint_grid")) == baseline, (
            f"the new grid painted while {blocked_action} was still pending"
        )
        assert not open_task.done(), (
            "handle_grid_command('open') returned while the pointer action "
            "was still pending"
        )
        release.set()
        assert await open_task is True
        assert len(_gui(c, "paint_grid")) > baseline, (
            "the released open never painted"
        )
        await _drain(c)

    return scenario()


def test_grid_reopen_waits_for_a_pending_click():
    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior["click_point"] = release
        assert await c.handle_grid_command("click", "tr-click") is True
        assert not c.grid_overlay_state.is_open
        await _wait_started(c, "click_point")
        await _assert_reopen_waits(c, release, "click_point")

    asyncio.run(scenario())


def test_grid_reopen_waits_for_a_pending_move():
    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior["move_pointer"] = release
        assert await c.handle_grid_command("move_here", "tr-move") is True
        await _wait_started(c, "move_pointer")
        await _assert_reopen_waits(c, release, "move_pointer")

    asyncio.run(scenario())


def test_grid_reopen_waits_for_a_pending_drag():
    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior["perform_drag"] = release
        assert await c.handle_grid_command("mark", "tr-mark") is True
        assert await c.handle_grid_command("drag", "tr-drag") is True
        await _wait_started(c, "perform_drag")
        await _assert_reopen_waits(c, release, "perform_drag")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. The numbered-overlay show path shares the fence.
# ---------------------------------------------------------------------------


def test_overlay_show_waits_for_a_pending_click():
    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior["click_point"] = release
        assert await c.handle_grid_command("click", "tr-click") is True
        await _wait_started(c, "click_point")

        show_task = asyncio.create_task(
            c.handle_overlay_command("show", "tr-show")
        )
        await asyncio.sleep(0.05)
        assert c.click_overlay_state.state is OverlayState.CLOSED, (
            "the numbered overlay opened while the click was still pending"
        )
        release.set()
        await show_task
        assert c.click_overlay_state.state is not OverlayState.CLOSED, (
            "the released show never opened the overlay"
        )
        await _drain(c)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 3. Unconfirmed channel: the session change is withheld, not risked.
# ---------------------------------------------------------------------------


async def _poison_channel(c):
    """A click whose reply AND whose probe time out: suspect stays set."""
    c._behavior["click_point"] = asyncio.TimeoutError()
    c._behavior["channel_probe"] = asyncio.TimeoutError()
    assert await c.handle_grid_command("click", "tr-poison") is True
    await _drain(c)
    assert c._grid_mouse_channel_suspect is True
    assert "grid_click_failed" in _reasons(c)


def test_grid_reopen_withheld_when_the_channel_stays_unconfirmed():
    async def scenario():
        c = _make_controller(grid_open=True)
        await _poison_channel(c)

        assert await c.handle_grid_command(
            "open", "tr-open", spoken="show grid",
        ) is True
        assert not _gui(c, "paint_grid"), (
            "the grid reopened over a channel that could still hold a "
            "queued pointer action"
        )
        assert not c.grid_overlay_state.is_open
        assert "grid_pointer_pending" in _reasons(c)
        await _drain(c)

    asyncio.run(scenario())


def test_overlay_show_withheld_when_the_channel_stays_unconfirmed():
    async def scenario():
        c = _make_controller(grid_open=True)
        await _poison_channel(c)

        await c.handle_overlay_command("show", "tr-show")
        assert c.click_overlay_state.state is OverlayState.CLOSED, (
            "the numbered overlay opened over an unconfirmed channel"
        )
        assert "grid_pointer_pending" in _reasons(c)
        await _drain(c)

    asyncio.run(scenario())


def test_auto_open_falls_back_to_the_notice_when_unconfirmed():
    # The ambiguous-click auto-open is the fourth way a new overlay
    # session becomes visible. With the channel unconfirmed it must NOT
    # auto-open; the plain ambiguous notice fires instead (the graceful
    # degrade the gate already has for every other failed condition).
    async def scenario():
        c = _make_controller(grid_open=True)
        await _poison_channel(c)

        from services.wheelhouse.ui.element_types import (
            ClickGesture,
            ElementQuery,
        )

        query = ElementQuery(
            "save", None, None, None, "save", gesture=ClickGesture.INVOKE,
        )
        await c.forward_click_element(query, "tr-byname")
        c._perform_auto_open_ambiguous.assert_not_called()
        ambiguous = [
            n for n in _notices(c) if n.get("outcome") == "ambiguous"
        ]
        assert ambiguous, "the plain ambiguous notice must still fire"
        await _drain(c)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 4. The fast path stays fast: nothing pending, no probe, no wait.
# ---------------------------------------------------------------------------


def test_wording_grid_pointer_pending_names_the_pending_action():
    from click_notice_toast_wording import compose_click_notice_wording
    from shared.click_notice import ClickNoticeEvent

    text = compose_click_notice_wording(
        ClickNoticeEvent(
            outcome="execution_failed",
            reason="grid_pointer_pending",
            matched_name=None,
            matched_names=(),
            spoken_name="",
            app_friendly_name="",
            snapshot_id=None,
            trace_id="tr",
        )
    )
    assert "mouse action" in text.lower()
    assert "try again" in text.lower()


def test_quiescent_reopen_paints_immediately():
    async def scenario():
        c = _make_controller(grid_open=False)
        assert await c.handle_grid_command(
            "open", "tr-open", spoken="show grid",
        ) is True
        assert _gui(c, "paint_grid")
        probe_calls = [
            a for (a, _p, _t) in c._captured["calls"]
            if a == "channel_probe"
        ]
        assert probe_calls == [], "no probe on a quiet channel"
        await _drain(c)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5. Whole-class coverage (wh-mouse-grid.1.30): by-name click_element and
#    numbered-badge click_snapshot_item requests are pointer-capable too
#    (non-default gestures and the coordinate fallback press real mouse
#    buttons in Input), so their unresolved failures must feed the same
#    fence: a timeout marks the channel suspect, and the badge-click
#    background task is registered like a grid pointer action.
# ---------------------------------------------------------------------------


def _byname_query():
    from services.wheelhouse.ui.element_types import (
        ClickGesture,
        ElementQuery,
    )

    # The gesture value is irrelevant to the Logic-side suspect marking
    # (Logic cannot know whether Input will use the gesture or coordinate
    # seam, so EVERY click_element timeout marks the channel).
    return ElementQuery(
        "save", None, None, None, "save", gesture=ClickGesture.INVOKE,
    )


def test_grid_reopen_withheld_after_a_named_click_timeout():
    # A by-name "click <target>" that times out leaves its click_element
    # request queued in Input, where it can still execute later. The
    # next grid open must probe first; with the probe also failing, the
    # open is withheld.
    async def scenario():
        c = _make_controller(grid_open=False)
        c._behavior["click_element"] = asyncio.TimeoutError()
        c._behavior["channel_probe"] = asyncio.TimeoutError()
        await c.forward_click_element(_byname_query(), "tr-byname")
        assert "timeout" in _reasons(c)
        assert getattr(c, "_grid_mouse_channel_suspect", False) is True, (
            "a timed-out by-name click must leave the pointer channel "
            "suspect"
        )

        assert await c.handle_grid_command(
            "open", "tr-open", spoken="show grid",
        ) is True
        assert not _gui(c, "paint_grid"), (
            "the grid opened over a channel that could still hold a "
            "queued by-name click"
        )
        assert not c.grid_overlay_state.is_open
        assert "grid_pointer_pending" in _reasons(c)
        await _drain(c)

    asyncio.run(scenario())


def test_grid_reopen_probes_and_opens_after_a_named_click_timeout():
    # Input executes commands in order, so one probe reply proves the
    # stale click_element left the pipeline: the open proceeds and the
    # suspect flag clears.
    async def scenario():
        c = _make_controller(grid_open=False)
        c._behavior["click_element"] = asyncio.TimeoutError()
        await c.forward_click_element(_byname_query(), "tr-byname")
        assert getattr(c, "_grid_mouse_channel_suspect", False) is True

        assert await c.handle_grid_command(
            "open", "tr-open", spoken="show grid",
        ) is True
        assert _gui(c, "paint_grid"), "the confirmed open must paint"
        assert c.grid_overlay_state.is_open
        assert c._grid_mouse_channel_suspect is False
        probe_calls = [
            a for (a, _p, _t) in c._captured["calls"]
            if a == "channel_probe"
        ]
        assert len(probe_calls) == 1
        await _drain(c)

    asyncio.run(scenario())


def test_grid_next_screen_withheld_after_a_named_click_timeout():
    # "grid next screen" is a fenced session change too. It is reachable
    # with a suspect channel precisely through a by-name click: grid cell
    # actions close the grid, but a by-name "click <target>" leaves it
    # open, so its timeout yields an open grid plus a suspect channel.
    async def scenario():
        c = _make_controller(grid_open=True)
        c._behavior["click_element"] = asyncio.TimeoutError()
        c._behavior["channel_probe"] = asyncio.TimeoutError()
        await c.forward_click_element(_byname_query(), "tr-byname")
        assert getattr(c, "_grid_mouse_channel_suspect", False) is True

        baseline = len(_gui(c, "paint_grid"))
        assert await c.handle_grid_command(
            "next_screen", "tr-next", spoken="grid next screen",
        ) is True
        assert len(_gui(c, "paint_grid")) == baseline, (
            "next_screen repainted over an unconfirmed channel"
        )
        assert c.grid_overlay_state.is_open, (
            "a withheld next_screen must leave the machine untouched"
        )
        assert "grid_pointer_pending" in _reasons(c)
        await _drain(c)

    asyncio.run(scenario())


def test_grid_reopen_waits_for_a_pending_numbered_badge_click():
    # A numbered-badge click ("click 3") is dispatched as a background
    # task; a grid open while it is queued or running must wait exactly
    # like a grid pointer action.
    async def scenario():
        c = _make_controller(grid_open=False)
        release = asyncio.Event()
        c._behavior["click_snapshot_item"] = release
        c._dispatch_snapshot_item_click(
            snapshot_id="snap-1", item_id="i1", trace_id="tr-badge",
        )
        await _wait_started(c, "click_snapshot_item")

        baseline = len(_gui(c, "paint_grid"))
        open_task = asyncio.create_task(
            c.handle_grid_command("open", "tr-open", spoken="show grid")
        )
        await asyncio.sleep(0.05)
        assert len(_gui(c, "paint_grid")) == baseline, (
            "the grid painted while the badge click was still pending"
        )
        assert not open_task.done()
        release.set()
        assert await open_task is True
        assert len(_gui(c, "paint_grid")) > baseline
        await _drain(c)

    asyncio.run(scenario())


def test_grid_reopen_withheld_after_a_numbered_badge_click_timeout():
    async def scenario():
        c = _make_controller(grid_open=False)
        c._behavior["click_snapshot_item"] = asyncio.TimeoutError()
        c._behavior["channel_probe"] = asyncio.TimeoutError()
        c._dispatch_snapshot_item_click(
            snapshot_id="snap-1", item_id="i1", trace_id="tr-badge",
        )
        await _drain(c)
        assert getattr(c, "_grid_mouse_channel_suspect", False) is True, (
            "a timed-out badge click must leave the pointer channel "
            "suspect"
        )

        assert await c.handle_grid_command(
            "open", "tr-open", spoken="show grid",
        ) is True
        assert not _gui(c, "paint_grid")
        assert "grid_pointer_pending" in _reasons(c)
        await _drain(c)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 6. The queued-before-lock window (wh-mouse-grid.1.31): a dispatched
#    task that has not yet run holds no lock, so only the pending-task
#    registration makes the fence wait for it. Each test runs the
#    session change INLINE with no yield after the dispatch, so the
#    fence executes before the dispatched task's first step.
# ---------------------------------------------------------------------------


async def _release_soon(release: asyncio.Event, delay: float = 0.2):
    await asyncio.sleep(delay)
    release.set()


def _assert_open_waits_for_unstarted(commands, action: str):
    """Dispatch ``action``, then run 'open' inline with no yield between.

    With the registration in place the fence awaits the still-unstarted
    task, so by the time 'open' returns the releaser has fired. With the
    registration removed the fence takes its fast path (the lock is
    free -- the task never ran) and 'open' returns, painted, long before
    the 0.2s release.
    """

    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior[action] = release
        releaser = asyncio.create_task(_release_soon(release))
        for args in commands:
            assert await c.handle_grid_command(*args) is True
        # No await between the dispatching command above and the open
        # below: the pointer task exists but has not started.
        assert c._pending_grid_mouse_actions, (
            "the dispatched task was not registered with the fence"
        )
        assert not c._started.get(action), (
            "the dispatched task ran before the fence; this test no "
            "longer exercises the queued-before-lock window"
        )
        baseline = len(_gui(c, "paint_grid"))
        assert await c.handle_grid_command(
            "open", "tr-open2", spoken="show grid",
        ) is True
        assert release.is_set(), (
            "open returned before the queued pointer action resolved"
        )
        assert len(_gui(c, "paint_grid")) > baseline, (
            "the fenced open never painted after the action resolved"
        )
        await releaser
        await _drain(c)

    asyncio.run(scenario())


def test_grid_reopen_waits_for_an_unstarted_click():
    _assert_open_waits_for_unstarted(
        [("click", "tr-c1")], "click_point",
    )


def test_grid_reopen_waits_for_an_unstarted_move():
    _assert_open_waits_for_unstarted(
        [("move_here", "tr-m1")], "move_pointer",
    )


def test_grid_reopen_waits_for_an_unstarted_drag():
    _assert_open_waits_for_unstarted(
        [("mark", "tr-k1"), ("drag", "tr-d1")], "perform_drag",
    )


def test_overlay_show_waits_for_an_unstarted_click():
    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior["click_point"] = release
        releaser = asyncio.create_task(_release_soon(release))
        assert await c.handle_grid_command("click", "tr-c") is True
        assert not c._started.get("click_point"), (
            "the dispatched task ran before the fence; this test no "
            "longer exercises the queued-before-lock window"
        )
        await c.handle_overlay_command("show", "tr-show")
        assert release.is_set(), (
            "show returned before the queued pointer action resolved"
        )
        assert c.click_overlay_state.state is not OverlayState.CLOSED
        await releaser
        await _drain(c)

    asyncio.run(scenario())


def test_auto_open_waits_for_an_unstarted_click():
    # The ambiguous auto-open gate is the fourth session opener; with a
    # pointer task queued it must wait, then auto-open normally.
    async def scenario():
        c = _make_controller(grid_open=True)
        release = asyncio.Event()
        c._behavior["click_point"] = release
        releaser = asyncio.create_task(_release_soon(release))
        assert await c.handle_grid_command("click", "tr-c") is True
        assert not c._started.get("click_point"), (
            "the dispatched task ran before the fence; this test no "
            "longer exercises the queued-before-lock window"
        )
        await c.forward_click_element(_byname_query(), "tr-byname")
        assert release.is_set(), (
            "the auto-open gate ran before the queued action resolved"
        )
        c._perform_auto_open_ambiguous.assert_called_once()
        await releaser
        await _drain(c)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 7. The transition window (wh-mouse-grid.1.32): after the fence passes,
#    grid open / next_screen awaits the monitor lookup; a badge click
#    dispatched DURING that await (the GUI badge-click event, the
#    held-click resolver -- both concurrent Logic tasks) would slip
#    behind the completed fence and execute under the freshly painted
#    grid. The admission barrier refuses such a dispatch outright: the
#    user's newest intent is the grid, and the click targets an overlay
#    the transition is tearing down.
# ---------------------------------------------------------------------------


def _park_monitor_lookup(c):
    """Replace the monitor lookup with one parked on an event."""
    reached = asyncio.Event()
    release = asyncio.Event()

    async def _ctx():
        reached.set()
        await release.wait()
        return ((MON,), MON)

    c._grid_monitor_context_off_loop = _ctx
    return reached, release


def _assert_transition_refuses_badge_click(command: str, *, grid_open: bool):
    async def scenario():
        c = _make_controller(grid_open=grid_open)
        reached, release = _park_monitor_lookup(c)
        cmd_task = asyncio.create_task(
            c.handle_grid_command(command, "tr-cmd", spoken="grid")
        )
        await asyncio.wait_for(reached.wait(), 1.0)

        c._dispatch_snapshot_item_click(
            snapshot_id="snap-1", item_id="i1", trace_id="tr-badge",
        )
        await _drain(c)
        sent = [
            a for (a, _p, _t) in c._captured["calls"]
            if a == "click_snapshot_item"
        ]
        assert not sent, (
            "a badge click dispatched during the grid transition was "
            "sent to Input"
        )
        assert not getattr(c, "_pending_grid_mouse_actions", None), (
            "the refused badge click left a task registered with the "
            "fence"
        )
        assert "grid_transition_pending" in _reasons(c)

        release.set()
        assert await cmd_task is True
        assert _gui(c, "paint_grid"), (
            "the released transition never painted"
        )
        await _drain(c)

    asyncio.run(scenario())


def test_open_refuses_a_badge_click_during_the_monitor_lookup():
    _assert_transition_refuses_badge_click("open", grid_open=False)


def test_next_screen_refuses_a_badge_click_during_the_monitor_lookup():
    _assert_transition_refuses_badge_click("next_screen", grid_open=True)


def test_a_badge_click_outside_a_transition_is_not_refused():
    # The barrier must clear when the transition ends: a badge click
    # AFTER the open completes dispatches normally.
    async def scenario():
        c = _make_controller(grid_open=False)
        assert await c.handle_grid_command(
            "open", "tr-open", spoken="grid",
        ) is True
        c._dispatch_snapshot_item_click(
            snapshot_id="snap-1", item_id="i1", trace_id="tr-badge",
        )
        await _drain(c)
        sent = [
            a for (a, _p, _t) in c._captured["calls"]
            if a == "click_snapshot_item"
        ]
        assert sent, "a badge click outside a transition must dispatch"
        assert "grid_transition_pending" not in _reasons(c)
        await _drain(c)

    asyncio.run(scenario())


def test_wording_grid_transition_pending():
    from click_notice_toast_wording import compose_click_notice_wording
    from shared.click_notice import ClickNoticeEvent

    text = compose_click_notice_wording(
        ClickNoticeEvent(
            outcome="execution_failed",
            reason="grid_transition_pending",
            matched_name=None,
            matched_names=(),
            spoken_name="",
            app_friendly_name="",
            snapshot_id=None,
            trace_id="tr",
        )
    )
    assert "grid" in text.lower()
    assert "click" in text.lower()


# ---------------------------------------------------------------------------
# 8. The barrier's whole scope (wh-mouse-grid.1.33): section 7 parks only
#    the monitor lookup, so a regression that clears the flag after that
#    first await (before the overlay teardown inside
#    _perform_grid_effects) or that clears it only on the happy path
#    would survive those tests. These tests pin the flag's full extent:
#    it stays up across the awaited overlay teardown (an open always
#    emits CLOSE_NUMBERED_OVERLAY first), and it comes down on EVERY
#    exit -- withheld, NO_MONITOR, a failed monitor lookup, and a
#    cancelled open -- so later badge clicks are admitted again.
#    next_screen has no teardown-park variant on purpose: NEXT_MONITOR
#    emits only paint effects (grid_overlay_state._on_next_monitor) and
#    _put_grid_gui_event is a synchronous put_nowait, so the monitor
#    lookup (covered in section 7) is that branch's only await.
# ---------------------------------------------------------------------------


def _badge_sends(c) -> list:
    return [
        a for (a, _p, _t) in c._captured["calls"]
        if a == "click_snapshot_item"
    ]


async def _assert_badge_admitted(c):
    """After a finished transition, a badge click must dispatch normally."""
    before = len(_badge_sends(c))
    c._dispatch_snapshot_item_click(
        snapshot_id="snap-1", item_id="i1", trace_id="tr-after",
    )
    await _drain(c)
    assert len(_badge_sends(c)) > before, (
        "a badge click after the transition ended was not sent to Input"
    )


def _paint_numbered_overlay(machine):
    """Drive the real overlay machine to PAINTED (the visible state)."""
    from services.wheelhouse.click_overlay_state import (
        OverlayEvent,
        OverlayEventKind,
        PaintAckState,
    )

    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = machine.overlay_session_id, machine.paint_generation
    machine.apply(
        OverlayEvent(
            OverlayEventKind.BUILD_RESPONSE,
            overlay_session_id=sess,
            paint_generation=gen,
            snapshot_id="snap-1",
        )
    )
    machine.apply(
        OverlayEvent(
            OverlayEventKind.PAINT_ACK,
            overlay_session_id=sess,
            paint_generation=gen,
            paint_state=PaintAckState.PAINTED,
        )
    )
    assert machine.state is OverlayState.PAINTED


def test_open_refuses_a_badge_click_during_the_overlay_teardown():
    # The barrier must survive PAST the monitor lookup, through BOTH
    # awaits of the teardown: production handle_overlay_command('hide')
    # returns a scheduled clear-batch task, and _perform_grid_effects
    # awaits that task separately before painting
    # (wh-mouse-grid.1.34: a regression clearing the flag between the
    # two awaits must fail here). The real hide branch runs against a
    # PAINTED overlay machine; only _perform_overlay_effects -- the seam
    # that schedules the clear batch -- is replaced, with one returning
    # a task parked on an event.
    async def scenario():
        c = _make_controller(grid_open=False)
        _paint_numbered_overlay(c.click_overlay_state)
        reached = asyncio.Event()
        release = asyncio.Event()

        def _parked_clear_batch(effects, *, trace_id):
            assert effects, "the hide produced no effects to perform"

            async def _batch():
                reached.set()
                await release.wait()

            return asyncio.get_event_loop().create_task(_batch())

        c._perform_overlay_effects = _parked_clear_batch
        baseline = len(_gui(c, "paint_grid"))
        open_task = asyncio.create_task(
            c.handle_grid_command("open", "tr-open", spoken="grid")
        )
        await asyncio.wait_for(reached.wait(), 1.0)

        c._dispatch_snapshot_item_click(
            snapshot_id="snap-1", item_id="i1", trace_id="tr-badge",
        )
        await asyncio.sleep(0)
        assert not _badge_sends(c), (
            "a badge click dispatched while the overlay clear batch was "
            "in flight was sent to Input"
        )
        assert "grid_transition_pending" in _reasons(c)
        assert len(_gui(c, "paint_grid")) == baseline, (
            "the grid painted before the overlay clear batch finished"
        )

        release.set()
        assert await open_task is True
        assert len(_gui(c, "paint_grid")) > baseline
        await _assert_badge_admitted(c)
        await _drain(c)

    asyncio.run(scenario())


def test_withheld_open_clears_the_barrier_for_later_badge_clicks():
    # The grid_pointer_pending early return exits through the finally:
    # the flag must be down and later badge clicks admitted, even though
    # no grid session became visible.
    async def scenario():
        c = _make_controller(grid_open=True)
        await _poison_channel(c)
        assert await c.handle_grid_command(
            "open", "tr-open", spoken="grid",
        ) is True
        assert "grid_pointer_pending" in _reasons(c)
        assert c._grid_session_transition is False
        await _assert_badge_admitted(c)
        await _drain(c)

    asyncio.run(scenario())


def _assert_no_monitor_clears_the_barrier(command: str, *, grid_open: bool):
    async def scenario():
        c = _make_controller(grid_open=grid_open)

        async def _no_monitors():
            return ((), None)

        c._grid_monitor_context_off_loop = _no_monitors
        assert await c.handle_grid_command(
            command, "tr-cmd", spoken="grid",
        ) is True
        assert "grid_no_monitor" in _reasons(c)
        assert c._grid_session_transition is False
        await _assert_badge_admitted(c)
        await _drain(c)

    asyncio.run(scenario())


def test_no_monitor_open_clears_the_barrier_for_later_badge_clicks():
    _assert_no_monitor_clears_the_barrier("open", grid_open=False)


def test_no_monitor_next_screen_clears_the_barrier_for_later_badge_clicks():
    _assert_no_monitor_clears_the_barrier("next_screen", grid_open=True)


def test_failed_monitor_lookup_clears_the_barrier_for_later_badge_clicks():
    # An unexpected exception between the fence and the paint must not
    # leave the flag stuck (every later badge click would be refused).
    async def scenario():
        c = _make_controller(grid_open=False)

        async def _broken_lookup():
            raise RuntimeError("monitor enumeration failed")

        c._grid_monitor_context_off_loop = _broken_lookup
        try:
            await c.handle_grid_command("open", "tr-open", spoken="grid")
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "the broken monitor lookup did not propagate"
            )
        assert c._grid_session_transition is False
        await _assert_badge_admitted(c)
        await _drain(c)

    asyncio.run(scenario())


def test_cancelled_open_clears_the_barrier_for_later_badge_clicks():
    # Cancellation mid-transition (Logic shutdown, task teardown) exits
    # through the finally too.
    async def scenario():
        c = _make_controller(grid_open=False)
        reached = asyncio.Event()
        release = asyncio.Event()

        async def _parked_lookup():
            reached.set()
            await release.wait()
            return ((MON,), MON)

        c._grid_monitor_context_off_loop = _parked_lookup
        open_task = asyncio.create_task(
            c.handle_grid_command("open", "tr-open", spoken="grid")
        )
        await asyncio.wait_for(reached.wait(), 1.0)
        open_task.cancel()
        try:
            await open_task
        except asyncio.CancelledError:
            pass
        assert c._grid_session_transition is False
        await _assert_badge_admitted(c)
        await _drain(c)

    asyncio.run(scenario())
