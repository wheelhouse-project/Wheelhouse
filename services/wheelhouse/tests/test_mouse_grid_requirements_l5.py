"""Mouse grid and pointer action requirements (area L5), at the boundary.

Bead: wh-full-refactor.6.1 (stage A of place 3). Each test pins one row MG-n of
the requirements list for the mouse grid methods of ``MouseGridController``
(services/wheelhouse/mouse_grid/controller.py; before wh-full-refactor.6.2
they were methods of ``LogicController`` in main.py) and is named after its row.

Every test drives a real Logic process built by tests/overlay_boundary_harness.py
and tests/mouse_grid_boundary_harness.py. The inputs are spoken words through a
real ``SpeechProcessor`` (``OverlayBoundary.say``), replies of the Input process
on the real response queue (``OverlayBoundary.reply``), the monitors and the
focused window as Windows reports them (``FakeMonitors``), config values, and
the passage of time (``OverlayBoundary.advance``, which moves the loop clock
that ``asyncio.wait_for`` in ``WheelHouseApp.send_request`` reads). A test
asserts only on what leaves Logic: the pointer commands for the Input process,
the grid messages and click notices for the GUI process, and the typed words.
Nothing here names a private member of a production class.
"""

from __future__ import annotations

import pytest

from tests.mouse_grid_boundary_harness import (
    PRIMARY,
    SECOND,
    FakeMonitors,
    Monitor,
    grid_gui,
    last_action_record,
    mouse_failed,
    mouse_grid_boundary,
    mouse_ok,
    notices,
    pointer_frames,
    probe_reply,
    quiet,
    typed,
)
from tests.overlay_boundary_harness import write_config

pytestmark = pytest.mark.journey

# response_timeout_ms of config.toml.example is 3000.
_TIMEOUT_S = 3.1
_ON_SECOND = (2000, 100, 2600, 700)


def _paint(monitor: Monitor, left, top, width, height) -> dict:
    return {
        "action": "paint_grid",
        "monitor_left": monitor.left, "monitor_top": monitor.top,
        "monitor_width": monitor.width, "monitor_height": monitor.height,
        "left": left, "top": top, "width": width, "height": height,
    }


def _full(monitor: Monitor) -> dict:
    return _paint(monitor, monitor.left, monitor.top, monitor.width, monitor.height)


def _actions(messages) -> list:
    return [m.get("action") for m in messages]


def _point(frame) -> dict:
    return {k: v for k, v in frame.params.items() if k != "trace_id"}


async def _open(b) -> None:
    mark = b.mark()
    await b.say("show grid")
    await b.flush()
    assert "paint_grid" in _actions(grid_gui(b, mark))


async def _clicked_and_timed_out(b) -> None:
    """Open the grid, say "click", leave the reply out, and let the wait end.

    Leaves the probe that follows the timeout unanswered too, so the channel
    stays suspect. Returns after the click's failure notice.
    """

    await _open(b)
    await b.say("click")
    await b.next_frame("click_point")
    await b.advance(_TIMEOUT_S)
    await b.next_frame("channel_probe")
    await b.advance(_TIMEOUT_S)
    await b.flush()


# ---------------------------------------------------------------------------
# Opening the grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window_rect, expected", [
    pytest.param((100, 100, 900, 700), PRIMARY, id="window-on-primary"),
    pytest.param(_ON_SECOND, SECOND, id="window-on-second"),
])
async def test_mg_1_show_grid_paints_the_focused_windows_monitor(
    tmp_path, window_rect, expected,
):
    fake = FakeMonitors(window_rect=window_rect)
    async with mouse_grid_boundary(tmp_path, monitors=fake) as (b, _):
        mark = b.mark()
        await b.say("show grid")
        frames, gui = await quiet(b, mark)
        assert gui == [_full(expected)]
        assert frames == []
        assert typed(b, mark) == []


async def test_mg_2_show_grid_hides_the_numbers_before_it_paints(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await b.show_numbers_painted()
        mark = b.mark()
        await b.say("show grid")
        _, gui = await quiet(b, mark)
        assert _actions(gui) == ["clear_overlay", "paint_grid"]


@pytest.mark.parametrize("monitors, window_rect", [
    pytest.param([SECOND, PRIMARY], None, id="window-rect-fails-origin-wins"),
    pytest.param(
        [Monitor(-1280, 0, 1280, 1024), Monitor(1280, 0, 800, 600)], None,
        id="window-rect-fails-no-origin-first-wins",
    ),
    pytest.param([PRIMARY, SECOND], (9000, 9000, 9100, 9100),
                 id="window-off-every-monitor-first-wins"),
])
async def test_mg_3_unknown_focused_monitor_opens_on_the_primary(
    tmp_path, monitors, window_rect,
):
    fake = FakeMonitors(monitors=monitors, window_rect=window_rect)
    async with mouse_grid_boundary(tmp_path, monitors=fake) as (b, _):
        mark = b.mark()
        await b.say("show grid")
        _, gui = await quiet(b, mark)
        expected = next(
            (m for m in monitors if (m.left, m.top) == (0, 0)), monitors[0],
        )
        assert gui == [_full(expected)]


@pytest.mark.parametrize("fake", [
    pytest.param(FakeMonitors(monitors=[]), id="no-monitor"),
    pytest.param(FakeMonitors(dpi_aware=False), id="dpi-context-refused"),
    pytest.param(FakeMonitors(enumeration_error=RuntimeError("no display")),
                 id="enumeration-raises"),
])
async def test_mg_4_no_usable_monitor_gives_a_notice_and_no_grid(tmp_path, fake):
    async with mouse_grid_boundary(tmp_path, monitors=fake) as (b, _):
        mark = b.mark()
        await b.say("show grid")
        frames, gui = await quiet(b, mark)
        assert _actions(gui) == ["show_click_notice"]
        assert notices(b, mark) == ["grid_no_monitor"]
        assert frames == []
        # The grid stayed closed: a grid-open-only word is typed.
        mark = b.mark()
        await b.say("mark")
        frames, gui = await quiet(b, mark)
        assert typed(b, mark) == ["mark"]
        assert gui == [] and frames == []


@pytest.mark.parametrize("click", [
    pytest.param({"enabled": False}, id="click-disabled"),
    pytest.param({"grid_min_cell_px": 0}, id="bad-grid-key"),
])
async def test_mg_5_disabled_grid_gives_one_notice_per_session(tmp_path, click):
    config = write_config(tmp_path, **click)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        mark = b.mark()
        await b.say("show grid")
        frames, gui = await quiet(b, mark)
        assert _actions(gui) == ["show_click_notice"]
        assert notices(b, mark) == ["disabled_by_config"]
        assert frames == [] and typed(b, mark) == []
        mark = b.mark()
        await b.say("show grid")
        frames, gui = await quiet(b, mark)
        assert gui == [] and frames == [] and typed(b, mark) == []


async def test_mg_6_by_name_click_and_show_grid_share_the_one_notice(tmp_path):
    config = write_config(tmp_path, enabled=False)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        mark = b.mark()
        # With the safety word, the by-name click shows its notice (without
        # it, the words are typed and the flag stays unset).
        await b.say("x-ray click cancel")
        await b.flush()
        assert notices(b, mark) == ["disabled_by_config"]
        mark = b.mark()
        await b.say("show grid")
        _, gui = await quiet(b, mark)
        assert gui == []


@pytest.mark.parametrize("words", ["hide grid", "grid next screen"])
async def test_mg_7_disabled_grid_hide_and_next_screen_do_nothing(tmp_path, words):
    config = write_config(tmp_path, enabled=False)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert gui == [] and frames == [] and typed(b, mark) == []


# ---------------------------------------------------------------------------
# Closing and moving the grid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("opened", [
    pytest.param(True, id="grid-open"),
    pytest.param(False, id="grid-closed"),
])
async def test_mg_8_hide_grid_sends_clear_grid(tmp_path, opened):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        if opened:
            await _open(b)
        mark = b.mark()
        await b.say("hide grid")
        frames, gui = await quiet(b, mark)
        assert gui == [{"action": "clear_grid"}]
        assert frames == [] and typed(b, mark) == []
        mark = b.mark()
        await b.say("mark")
        await quiet(b, mark)
        assert typed(b, mark) == ["mark"]


async def test_mg_9_next_screen_with_the_grid_closed_does_nothing(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        mark = b.mark()
        await b.say("grid next screen")
        frames, gui = await quiet(b, mark)
        assert gui == [] and frames == [] and typed(b, mark) == []


async def test_mg_9_next_screen_with_the_grid_closed_does_not_probe_a_suspect_channel(
    tmp_path,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        start = b.mark()
        await _clicked_and_timed_out(b)
        # The click closed the grid before it sent click_point (MG-20).
        grid = [a for a in _actions(grid_gui(b, start))
                if a in ("paint_grid", "paint_grid_pin", "clear_grid")]
        assert grid[-1] == "clear_grid"
        mark = b.mark()
        b.say_soon("grid next screen")
        frames, gui = await quiet(b, mark)
        assert frames == []
        assert gui == [] and notices(b, mark) == []
        assert typed(b, mark) == []


@pytest.mark.parametrize("monitors, steps", [
    pytest.param([PRIMARY, SECOND], [SECOND, PRIMARY], id="moves-then-wraps"),
    pytest.param([PRIMARY], [PRIMARY], id="one-monitor-restarts"),
])
async def test_mg_10_next_screen_paints_the_next_monitor(tmp_path, monitors, steps):
    fake = FakeMonitors(monitors=monitors)
    async with mouse_grid_boundary(tmp_path, monitors=fake) as (b, _):
        await _open(b)
        await b.say("5")
        for expected in steps:
            mark = b.mark()
            await b.say("grid next screen")
            frames, gui = await quiet(b, mark)
            assert gui == [_full(expected)]
            assert frames == []


async def test_mg_10b_next_screen_from_a_monitor_that_is_gone_goes_to_the_first(
    tmp_path,
):
    fake = FakeMonitors(monitors=[PRIMARY, SECOND])
    async with mouse_grid_boundary(tmp_path, monitors=fake) as (b, _):
        await _open(b)
        third = Monitor(-1024, 0, 1024, 768)
        fake.monitors = [third, SECOND]
        mark = b.mark()
        await b.say("grid next screen")
        _, gui = await quiet(b, mark)
        assert gui == [_full(third)]


@pytest.mark.parametrize("words, expected", [
    pytest.param("show grid", PRIMARY, id="show-grid-again"),
    pytest.param("grid next screen", SECOND, id="next-screen"),
])
async def test_mg_11_a_restart_repaints_the_mark(tmp_path, words, expected):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        await b.say("1")
        await b.say("mark")
        await b.say("9")
        mark = b.mark()
        await b.say(words)
        _, gui = await quiet(b, mark)
        assert gui == [
            _full(expected), {"action": "paint_grid_pin", "x": 320, "y": 180},
        ]


@pytest.mark.parametrize("change", [
    pytest.param("no-monitor", id="next-screen-no-monitor"),
    pytest.param("dpi-refused", id="show-grid-dpi-refused"),
])
async def test_mg_12_a_failed_restart_leaves_the_open_grid_as_it_was(
    tmp_path, change,
):
    async with mouse_grid_boundary(tmp_path) as (b, fake):
        await _open(b)
        await b.say("5")
        if change == "no-monitor":
            fake.monitors = []
            words = "grid next screen"
        else:
            fake.dpi_aware = False
            words = "show grid"
        mark = b.mark()
        await b.say(words)
        _, gui = await quiet(b, mark)
        assert notices(b, mark) == ["grid_no_monitor"]
        assert _actions(gui) == ["show_click_notice"]
        # Still open on the cell 5 of the primary: "5" paints its cell 5.
        mark = b.mark()
        await b.say("5")
        _, gui = await quiet(b, mark)
        assert gui == [_paint(PRIMARY, 853, 480, 213, 120)]


# ---------------------------------------------------------------------------
# Words with the grid open and closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("words", ["mark", "drag", "move here", "go here", "5"])
async def test_mg_13_grid_words_with_the_grid_closed_are_typed(tmp_path, words):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert typed(b, mark) == [words]
        assert frames == [] and gui == []


@pytest.mark.parametrize("words, cell", [
    pytest.param("1", (0, 0, 640, 360), id="one"),
    pytest.param("five", (640, 360, 640, 360), id="five"),
    pytest.param("number nine", (1280, 720, 640, 360), id="number-nine"),
])
async def test_mg_14_a_number_paints_its_cell(tmp_path, words, cell):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert gui == [_paint(PRIMARY, *cell)]
        assert frames == [] and typed(b, mark) == []


async def test_mg_15_a_number_at_the_minimum_cell_does_nothing(tmp_path):
    config = write_config(tmp_path, grid_min_cell_px=500)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        await _open(b)
        await b.say("5")
        mark = b.mark()
        await b.say("5")
        frames, gui = await quiet(b, mark)
        assert gui == [] and frames == [] and typed(b, mark) == []


async def test_mg_16_mark_pins_the_center_and_starts_the_grid_over(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        await b.say("1")
        mark = b.mark()
        await b.say("mark")
        frames, gui = await quiet(b, mark)
        assert gui == [
            {"action": "paint_grid_pin", "x": 320, "y": 180}, _full(PRIMARY),
        ]
        assert frames == []


async def test_mg_17_drag_without_a_mark_gives_a_notice(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        mark = b.mark()
        await b.say("drag")
        frames, gui = await quiet(b, mark)
        assert notices(b, mark) == ["drag_without_mark"]
        assert _actions(gui) == ["show_click_notice"]
        assert frames == []
        # The grid stays open.
        mark = b.mark()
        await b.say("5")
        _, gui = await quiet(b, mark)
        assert _actions(gui) == ["paint_grid"]


@pytest.mark.parametrize("duration_ms", [250, 400])
async def test_mg_18_drag_with_a_mark_clears_and_drags(tmp_path, duration_ms):
    config = write_config(tmp_path, drag_duration_ms=duration_ms)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        await _open(b)
        await b.say("1")
        await b.say("mark")
        await b.say("9")
        mark = b.mark()
        await b.say("drag")
        frames, gui = await quiet(b, mark)
        assert gui == [{"action": "clear_grid"}]
        assert [(f.action, _point(f)) for f in frames] == [("perform_drag", {
            "start_x": 320, "start_y": 180, "end_x": 1600, "end_y": 900,
            "duration_ms": duration_ms,
        })]


@pytest.mark.parametrize("close_words", ["hide grid", "click", "move here"])
async def test_mg_19_a_closed_grid_forgets_its_mark(tmp_path, close_words):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        await b.say("mark")
        await b.say(close_words)
        for frame in pointer_frames(b, (0, 0)):
            b.reply(frame, mouse_ok(frame))
        await _open(b)
        mark = b.mark()
        await b.say("drag")
        frames, _ = await quiet(b, mark)
        assert notices(b, mark) == ["drag_without_mark"]
        assert frames == []


@pytest.mark.parametrize("words, button, count", [
    pytest.param("click", "left", 1, id="click"),
    pytest.param("tap", "left", 1, id="tap"),
    pytest.param("right click", "right", 1, id="right"),
    pytest.param("double click", "left", 2, id="double"),
    pytest.param("triple click", "left", 3, id="triple"),
])
async def test_mg_20_click_clears_the_grid_and_clicks_the_center(
    tmp_path, words, button, count,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        await b.say("1")
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert gui == [{"action": "clear_grid"}]
        assert [(f.action, _point(f)) for f in frames] == [("click_point", {
            "x": 320, "y": 180, "button": button, "click_count": count,
        })]
        assert typed(b, mark) == []


@pytest.mark.parametrize("words, button", [
    pytest.param("click 5", "left", id="click-5"),
    pytest.param("right click 9", "right", id="right-click-9"),
])
async def test_mg_21_click_and_a_number_clicks_the_cell_without_painting_it(
    tmp_path, words, button,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert gui == [{"action": "clear_grid"}]
        center = {"click 5": (960, 540), "right click 9": (1600, 900)}[words]
        assert [(f.action, _point(f)) for f in frames] == [("click_point", {
            "x": center[0], "y": center[1], "button": button, "click_count": 1,
        })]


async def test_mg_22_click_and_a_number_at_the_minimum_cell_clicks_the_center(
    tmp_path,
):
    config = write_config(tmp_path, grid_min_cell_px=500)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        await _open(b)
        await b.say("1")
        mark = b.mark()
        await b.say("click 9")
        frames, gui = await quiet(b, mark)
        assert gui == [{"action": "clear_grid"}]
        assert [_point(f) for f in frames] == [
            {"x": 320, "y": 180, "button": "left", "click_count": 1},
        ]


@pytest.mark.parametrize("words", ["move here", "go here"])
async def test_mg_23_move_here_clears_the_grid_and_moves_the_pointer(
    tmp_path, words,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _open(b)
        await b.say("9")
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert gui == [{"action": "clear_grid"}]
        assert [(f.action, _point(f)) for f in frames] == [
            ("move_pointer", {"x": 1600, "y": 900}),
        ]
        assert typed(b, mark) == []


# ---------------------------------------------------------------------------
# The click at the pointer (grid closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("words, button, count", [
    pytest.param("click", "left", 1, id="click"),
    pytest.param("tap.", "left", 1, id="tap-period"),
    pytest.param("right click", "right", 1, id="right"),
    pytest.param("double click", "left", 2, id="double"),
    pytest.param("triple click", "left", 3, id="triple"),
])
async def test_mg_24_a_bare_click_word_clicks_at_the_pointer(
    tmp_path, words, button, count,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        mark = b.mark()
        await b.say(words)
        frames, gui = await quiet(b, mark)
        assert [(f.action, _point(f)) for f in frames] == [
            ("click_at_pointer", {"button": button, "click_count": count}),
        ]
        assert gui == [] and typed(b, mark) == []


async def test_mg_25_a_bare_click_word_is_typed_when_clicking_is_off(tmp_path):
    config = write_config(tmp_path, enabled=False)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        mark = b.mark()
        await b.say("click")
        frames, gui = await quiet(b, mark)
        assert typed(b, mark) == ["click"]
        assert frames == [] and gui == []


async def test_mg_26_a_bad_grid_key_keeps_the_click_at_the_pointer(tmp_path):
    config = write_config(tmp_path, grid_min_cell_px=0)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        mark = b.mark()
        await b.say("click")
        frames, _ = await quiet(b, mark)
        assert [f.action for f in frames] == ["click_at_pointer"]
        assert typed(b, mark) == []


# ---------------------------------------------------------------------------
# Replies of the Input process
# ---------------------------------------------------------------------------


async def _send(b, kind: str):
    """Make Logic send one pointer action of ``kind``; return its frame."""

    if kind == "click_at_pointer":
        await b.say("click")
        return await b.next_frame("click_at_pointer")
    if kind == "right_click_at_pointer":
        await b.say("right click")
        return await b.next_frame("click_at_pointer")
    await _open(b)
    if kind == "click_point":
        await b.say("click")
    elif kind == "right_click_point":
        await b.say("right click")
    elif kind == "move_pointer":
        await b.say("move here")
    else:
        await b.say("mark")
        await b.say("drag")
    action = {"right_click_point": "click_point"}.get(kind, kind)
    return await b.next_frame(action)


_FAILED = {
    "click_point": "grid_click_failed",
    "move_pointer": "grid_move_failed",
    "perform_drag": "grid_drag_failed",
    "click_at_pointer": "pointer_click_failed",
}


@pytest.mark.parametrize("kind", list(_FAILED))
async def test_mg_27_an_ok_reply_gives_no_notice(tmp_path, kind):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        mark = b.mark()
        b.reply(frame, mouse_ok(frame))
        frames, gui = await quiet(b, mark)
        assert gui == [] and frames == []


@pytest.mark.parametrize("kind", list(_FAILED))
async def test_mg_28_a_failed_reply_gives_the_actions_notice(tmp_path, kind):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        mark = b.mark()
        b.reply(frame, mouse_failed(frame, "send_input_failed"))
        await quiet(b, mark)
        assert notices(b, mark) == [_FAILED[kind]]


@pytest.mark.parametrize("kind, reason", [
    pytest.param("click_point", "grid_button_release_failed", id="click-left"),
    pytest.param("right_click_point", "grid_right_button_release_failed",
                 id="click-right"),
    pytest.param("perform_drag", "grid_button_release_failed", id="drag"),
    pytest.param("click_at_pointer", "grid_button_release_failed",
                 id="pointer-left"),
    pytest.param("right_click_at_pointer", "grid_right_button_release_failed",
                 id="pointer-right"),
])
async def test_mg_29_release_failed_names_the_button(tmp_path, kind, reason):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        mark = b.mark()
        b.reply(frame, mouse_failed(frame, "release_failed"))
        await quiet(b, mark)
        assert notices(b, mark) == [reason]


@pytest.mark.parametrize("kind", ["click_point", "click_at_pointer"])
async def test_mg_30_disabled_by_config_reply_gives_that_notice(tmp_path, kind):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        mark = b.mark()
        b.reply(frame, mouse_failed(frame, "disabled_by_config"))
        await quiet(b, mark)
        assert notices(b, mark) == ["disabled_by_config"]


@pytest.mark.parametrize("kind", ["click_point", "perform_drag"])
async def test_mg_31_a_malformed_reply_gives_the_actions_notice(tmp_path, kind):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        mark = b.mark()
        # No "outcome": MouseActionResponse.from_dict refuses the reply.
        b.reply(frame, {"status": "ok", "trace_id": frame.params["trace_id"]})
        await quiet(b, mark)
        assert notices(b, mark) == [_FAILED[kind]]


async def test_mg_32_an_error_reply_gives_the_notice_and_the_next_action_probes(
    tmp_path,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, "click_point")
        mark = b.mark()
        b.reply(frame, {"error": True, "message": "input handler raised"})
        await quiet(b, mark)
        assert notices(b, mark) == ["grid_click_failed"]
        mark = b.mark()
        await b.say("click")
        frames, _ = await quiet(b, mark)
        assert [f.action for f in frames] == ["channel_probe"]


# ---------------------------------------------------------------------------
# No reply: the timeout and the channel probe
# ---------------------------------------------------------------------------


async def test_mg_33_no_reply_in_time_sends_a_channel_probe(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, "click_point")
        mark = b.mark()
        # The loop clock also moves with real time, so the check before the
        # limit stays well under it.
        await b.advance(2.0)
        await b.flush()
        assert pointer_frames(b, mark) == []
        await b.advance(1.1)
        probe = await b.next_frame("channel_probe", timeout_s=2.0)
        assert probe.params == {"trace_id": frame.params["trace_id"]}


async def test_mg_33b_a_drag_waits_its_duration_longer(tmp_path):
    config = write_config(tmp_path, drag_duration_ms=2000)
    async with mouse_grid_boundary(tmp_path, config_path=config) as (b, _):
        await _send(b, "perform_drag")
        mark = b.mark()
        await b.advance(_TIMEOUT_S)
        frames, _ = await quiet(b, mark)
        assert frames == []
        await b.advance(2.0)
        await b.next_frame("channel_probe", timeout_s=2.0)


@pytest.mark.parametrize("kind, record, expected", [
    pytest.param("click_point", "succeeded", [], id="late-success"),
    pytest.param("click_point", "release_failed",
                 ["grid_button_release_failed"], id="late-release-left"),
    pytest.param("right_click_point", "release_failed",
                 ["grid_right_button_release_failed"], id="late-release-right"),
    pytest.param("click_point", "other_reason", ["grid_click_failed"],
                 id="late-other-reason"),
    pytest.param("click_point", "other_trace", ["grid_click_failed"],
                 id="stale-record"),
    pytest.param("move_pointer", "none", ["grid_move_failed"], id="no-record"),
])
async def test_mg_34_the_probe_reply_decides_the_timeout_notice(
    tmp_path, kind, record, expected,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        trace = frame.params["trace_id"]
        await b.advance(_TIMEOUT_S)
        probe = await b.next_frame("channel_probe")
        last = {
            "succeeded": last_action_record(
                frame.action, trace_id=trace, succeeded=True),
            "release_failed": last_action_record(
                frame.action, trace_id=trace, succeeded=False,
                reason="release_failed"),
            "other_reason": last_action_record(
                frame.action, trace_id=trace, succeeded=False,
                reason="send_input_failed"),
            "other_trace": last_action_record(
                frame.action, trace_id="an-older-trace", succeeded=True),
            "none": None,
        }[record]
        mark = b.mark()
        b.reply(probe, probe_reply(probe, last))
        frames, _ = await quiet(b, mark)
        assert notices(b, mark) == expected
        assert frames == []


async def test_mg_35_an_unanswered_probe_gives_the_failure_notice(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _send(b, "click_point")
        await b.advance(_TIMEOUT_S)
        await b.next_frame("channel_probe")
        mark = b.mark()
        await b.advance(_TIMEOUT_S)
        frames, _ = await quiet(b, mark)
        assert notices(b, mark) == ["grid_click_failed"]
        assert frames == []


@pytest.mark.parametrize("answered", [
    pytest.param(True, id="probe-answered"),
    pytest.param(False, id="probe-unanswered"),
])
async def test_mg_36_the_next_action_on_a_suspect_channel_probes_first(
    tmp_path, answered,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _clicked_and_timed_out(b)
        mark = b.mark()
        await b.say("right click")
        probe = await b.next_frame("channel_probe")
        frames, _ = await quiet(b, mark)
        assert [f.action for f in frames] == ["channel_probe"]
        if answered:
            b.reply(probe, probe_reply(probe))
            click = await b.next_frame("click_at_pointer")
            assert _point(click) == {"button": "right", "click_count": 1}
            b.reply(click, mouse_ok(click))
            await quiet(b, mark)
            assert notices(b, mark) == []
            # The answered probe cleared the suspicion: no second probe.
            mark = b.mark()
            await b.say("click")
            frames, _ = await quiet(b, mark)
            assert [f.action for f in frames] == ["click_at_pointer"]
        else:
            await b.advance(_TIMEOUT_S)
            frames, _ = await quiet(b, mark)
            assert [f.action for f in frames] == ["channel_probe"]
            assert notices(b, mark) == ["pointer_click_failed"]


@pytest.mark.parametrize("answered", [
    pytest.param(True, id="probe-answered"),
    pytest.param(False, id="probe-unanswered"),
])
async def test_mg_37_show_grid_on_a_suspect_channel_probes_first(
    tmp_path, answered,
):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        await _clicked_and_timed_out(b)
        mark = b.mark()
        b.say_soon("show grid")
        probe = await b.next_frame("channel_probe")
        frames, gui = await quiet(b, mark)
        assert [f.action for f in frames] == ["channel_probe"]
        assert gui == []
        if answered:
            b.reply(probe, probe_reply(probe))
            await b.next_gui("paint_grid")
            await quiet(b, mark)
            assert notices(b, mark) == []
        else:
            await b.advance(_TIMEOUT_S)
            frames, gui = await quiet(b, mark)
            assert _actions(gui) == ["show_click_notice"]
            assert notices(b, mark) == ["grid_pointer_pending"]
            assert [f.action for f in frames] == ["channel_probe"]


@pytest.mark.parametrize("kind", ["click_point", "perform_drag", "click_at_pointer"])
async def test_mg_38_show_grid_waits_for_a_pending_pointer_action(tmp_path, kind):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        frame = await _send(b, kind)
        mark = b.mark()
        b.say_soon("show grid")
        _, gui = await quiet(b, mark)
        assert "paint_grid" not in _actions(gui)
        b.reply(frame, mouse_ok(frame))
        await b.next_gui("paint_grid")
        frames, _ = await quiet(b, mark)
        assert frames == []


async def test_mg_39_one_pointer_action_at_a_time(tmp_path):
    async with mouse_grid_boundary(tmp_path) as (b, _):
        first = await _send(b, "click_point")
        mark = b.mark()
        await b.say("double click")
        frames, _ = await quiet(b, mark)
        assert frames == []
        b.reply(first, mouse_ok(first))
        second = await b.next_frame("click_at_pointer")
        assert _point(second) == {"button": "left", "click_count": 2}


# ---------------------------------------------------------------------------
# The DPI context of the monitor read (wh-full-refactor.6.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window_rect, expected", [
    pytest.param((100, 100, 900, 700), PRIMARY, id="window-on-primary"),
    pytest.param(_ON_SECOND, SECOND, id="window-on-second"),
])
async def test_mg_47_show_grid_on_a_scaled_desktop_paints_physical_pixels(
    tmp_path, window_rect, expected,
):
    # A thread outside the per-monitor DPI context reads every monitor and
    # window rectangle at half size, as Windows virtualizes them for the
    # DPI-unaware Logic process on a 200 percent desktop.
    fake = FakeMonitors(window_rect=window_rect, virtualized_scale=2)
    async with mouse_grid_boundary(tmp_path, monitors=fake) as (b, _):
        mark = b.mark()
        await b.say("show grid")
        frames, gui = await quiet(b, mark)
        assert gui == [_full(expected)]
        assert frames == []
