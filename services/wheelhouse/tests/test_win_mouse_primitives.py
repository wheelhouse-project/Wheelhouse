"""Unit tests for the SendInput-backed grid mouse primitives
(wh-input-mouse-primitives).

``utils.win_input_sender`` gains three primitives for the mouse-grid overlay
(``docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md``, the
"Input process -- the only place that touches the mouse" section):

* :func:`click_point` -- click at a physical-pixel point with a button
  (left / right) and a click count (1 or 2). Raw SendInput; no UI Automation
  and no occlusion hit-test, because the user picked the point visually (the
  spec's "Verification difference" section).
* :func:`move_pointer_to` -- park the pointer at a point, pressing nothing.
* :func:`drag_pointer` -- button down at a start point, interpolated movement
  across a duration, button up at the end. The release is guaranteed by a
  try/finally so a failure mid-movement can never leave the button pressed.

These tests fake ``user32`` (SendInput / GetSystemMetrics / GetCursorPos) and
the module's ``time`` reference, so they run headless with no real input
synthesis and no real sleeping. They assert the normalization math (including
a virtual desktop whose origin is negative -- a monitor left of or above the
primary), the MOUSEEVENTF flags on the captured Input structs, the
cursor-verify fail-closed path, the argument validation, and above all the
guaranteed button release on the drag failure paths.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from utils import win_input_sender as wis


# Virtual-desktop box used by most tests: origin (0, 0), size (1921, 1081) so
# the span-1 divisor is a clean 1920 / 1080. A physical point of (960, 540)
# normalizes to 960*65535/1920 = 32767.5 -> 32768 (round-half-up) on both axes.
VS_ORIGIN_X = 0
VS_ORIGIN_Y = 0
VS_WIDTH = 1921
VS_HEIGHT = 1081


class FakeUser32:
    """Records SendInput batches and serves scripted metrics / cursor reads.

    The cursor is modelled as "wherever the last MOVE event asked for", so a
    move+verify sequence succeeds by default. ``cursor_lands`` set to False
    makes every read report a far-away point, driving the fail-closed path.
    """

    def __init__(
        self,
        *,
        cursor_lands: bool = True,
        metrics: Optional[dict[int, int]] = None,
        send_returns: Optional[list[int]] = None,
        raise_on_batch: Optional[int] = None,
        blockinput_return: int = 1,
    ) -> None:
        self._cursor_lands = cursor_lands
        # Scripted SendInput return values, one per batch; once exhausted the
        # fake accepts every event of the batch. An entry of ``None`` also
        # means "accept everything".
        self._send_returns = send_returns or []
        # 0-based index of the SendInput batch that raises, or None.
        self._raise_on_batch = raise_on_batch
        self._blockinput_return = blockinput_return
        self._metrics = metrics or {
            wis.SM_XVIRTUALSCREEN: VS_ORIGIN_X,
            wis.SM_YVIRTUALSCREEN: VS_ORIGIN_Y,
            wis.SM_CXVIRTUALSCREEN: VS_WIDTH,
            wis.SM_CYVIRTUALSCREEN: VS_HEIGHT,
        }
        # The cursor position the next GetCursorPos reports, in PHYSICAL px.
        self._cursor = (0, 0)
        # Captured SendInput batches: list of list-of-dicts (one per event).
        self.sendinput_batches: list[list[dict[str, int]]] = []
        self.blockinput_calls: list[int] = []
        self.getcursorpos_calls = 0

    # --- Win32 surface -----------------------------------------------------

    def GetSystemMetrics(self, index: int) -> int:
        return self._metrics[index]

    def GetCursorPos(self, lp_point: Any) -> int:
        point = lp_point._obj  # type: ignore[attr-defined]
        self.getcursorpos_calls += 1
        if self._cursor_lands:
            point.x, point.y = self._cursor
        else:
            point.x, point.y = (-9999, -9999)
        return 1

    def BlockInput(self, flag: int) -> int:
        self.blockinput_calls.append(int(flag))
        return self._blockinput_return

    def SendInput(self, num: int, lp_array: Any, _size: int) -> int:
        index = len(self.sendinput_batches)
        array = lp_array._obj  # type: ignore[attr-defined]
        batch: list[dict[str, int]] = []
        for i in range(num):
            ev = array[i]
            batch.append(
                {
                    "type": int(ev.type),
                    "dx": int(ev.ii.mi.dx),
                    "dy": int(ev.ii.mi.dy),
                    "flags": int(ev.ii.mi.dwFlags),
                }
            )
        self.sendinput_batches.append(batch)

        # A MOVE event teleports the modelled cursor, so the verify sees it.
        for event in batch:
            if event["flags"] & wis.MOUSEEVENTF_MOVE:
                self._cursor = self._denormalize(event["dx"], event["dy"])

        if self._raise_on_batch is not None and index == self._raise_on_batch:
            raise OSError("SendInput failed at the platform boundary")

        if index < len(self._send_returns):
            scripted = self._send_returns[index]
            if scripted is not None:
                return scripted
        return num

    # --- helpers -----------------------------------------------------------

    def _denormalize(self, nx: int, ny: int) -> tuple[int, int]:
        """Invert the 0..65535 normalization back to a physical pixel."""
        ox = self._metrics[wis.SM_XVIRTUALSCREEN]
        oy = self._metrics[wis.SM_YVIRTUALSCREEN]
        w = self._metrics[wis.SM_CXVIRTUALSCREEN]
        h = self._metrics[wis.SM_CYVIRTUALSCREEN]
        px = ox + int(round(nx * (w - 1) / 65535.0)) if w > 1 else ox
        py = oy + int(round(ny * (h - 1) / 65535.0)) if h > 1 else oy
        return px, py

    def move_batches(self) -> list[list[dict[str, int]]]:
        return [
            b for b in self.sendinput_batches
            if all(e["flags"] & wis.MOUSEEVENTF_MOVE for e in b)
        ]

    def button_batches(self) -> list[list[dict[str, int]]]:
        return [
            b for b in self.sendinput_batches
            if any(not (e["flags"] & wis.MOUSEEVENTF_MOVE) for e in b)
        ]


class OrderRecordingUser32(FakeUser32):
    """FakeUser32 that also records BlockInput/SendInput interleaving."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ops: list[str] = []

    def BlockInput(self, flag: int) -> int:
        self.ops.append(f"block:{int(flag)}")
        return super().BlockInput(flag)

    def SendInput(self, num: int, lp_array: Any, size: int) -> int:
        result = super().SendInput(num, lp_array, size)
        batch = self.sendinput_batches[-1]
        if any(e["flags"] & wis.MOUSEEVENTF_LEFTDOWN for e in batch):
            self.ops.append("send:down")
        elif any(e["flags"] & wis.MOUSEEVENTF_LEFTUP for e in batch):
            self.ops.append("send:up")
        else:
            self.ops.append("send:move")
        return result


class LandsOnSecondMoveUser32(FakeUser32):
    """The first MOVE's landing is never observed; later ones are.

    Models the real SendInput/GetCursorPos race: the MOVE is posted
    asynchronously, so every verify poll after the first MOVE reads the stale
    pre-move position, and only a re-sent MOVE is observed to land.
    """

    def GetCursorPos(self, lp_point: Any) -> int:
        if len(self.move_batches()) < 2:
            point = lp_point._obj  # type: ignore[attr-defined]
            self.getcursorpos_calls += 1
            point.x, point.y = (-9999, -9999)
            return 1
        return super().GetCursorPos(lp_point)


class FakeTime:
    """Stand-in for the module's ``time`` reference; records sleeps."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def perf_counter(self) -> float:  # pragma: no cover - unused here
        return 0.0

    def monotonic(self) -> float:  # pragma: no cover - unused here
        return 0.0


@pytest.fixture
def patch_win32(monkeypatch):
    """Install a FakeUser32 + FakeTime onto the module; return a setter."""

    fake_time = FakeTime()

    def _install(fake: FakeUser32) -> tuple[FakeUser32, FakeTime]:
        monkeypatch.setattr(wis, "user32", fake)
        monkeypatch.setattr(wis, "time", fake_time)
        monkeypatch.setattr(
            wis,
            "kernel32",
            type("K", (), {"GetLastError": staticmethod(lambda: 0)})(),
        )
        return fake, fake_time

    return _install


# ---------------------------------------------------------------------------
# click_point
# ---------------------------------------------------------------------------


def test_click_point_left_single_moves_then_clicks(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.click_point(960, 540)

    assert ok is True
    assert reason is None
    moves = fake.move_batches()
    buttons = fake.button_batches()
    assert len(moves) == 1
    assert len(buttons) == 1

    move = moves[0][0]
    assert move["type"] == wis.INPUT_MOUSE
    assert move["dx"] == 32768 and move["dy"] == 32768
    assert move["flags"] == (
        wis.MOUSEEVENTF_MOVE
        | wis.MOUSEEVENTF_ABSOLUTE
        | wis.MOUSEEVENTF_VIRTUALDESK
    )

    down, up = buttons[0]
    assert down["flags"] == (
        wis.MOUSEEVENTF_LEFTDOWN
        | wis.MOUSEEVENTF_ABSOLUTE
        | wis.MOUSEEVENTF_VIRTUALDESK
    )
    assert up["flags"] == (
        wis.MOUSEEVENTF_LEFTUP
        | wis.MOUSEEVENTF_ABSOLUTE
        | wis.MOUSEEVENTF_VIRTUALDESK
    )
    assert down["dx"] == 32768 and down["dy"] == 32768


def test_click_point_right_button_uses_right_flags(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.click_point(960, 540, button="right")

    assert (ok, reason) == (True, None)
    down, up = fake.button_batches()[0]
    assert down["flags"] & wis.MOUSEEVENTF_RIGHTDOWN
    assert up["flags"] & wis.MOUSEEVENTF_RIGHTUP
    assert not (down["flags"] & wis.MOUSEEVENTF_LEFTDOWN)


def test_click_point_double_sends_two_down_up_pairs(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.click_point(960, 540, click_count=2)

    assert (ok, reason) == (True, None)
    batch = fake.button_batches()[0]
    assert len(batch) == 4
    assert batch[0]["flags"] & wis.MOUSEEVENTF_LEFTDOWN
    assert batch[1]["flags"] & wis.MOUSEEVENTF_LEFTUP
    assert batch[2]["flags"] & wis.MOUSEEVENTF_LEFTDOWN
    assert batch[3]["flags"] & wis.MOUSEEVENTF_LEFTUP


@pytest.mark.parametrize("button", ["middle", "LEFT", "", None, 1])
def test_click_point_rejects_unknown_button(patch_win32, button):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.click_point(960, 540, button=button)

    assert ok is False
    assert reason == "invalid_button"
    assert fake.sendinput_batches == []


@pytest.mark.parametrize("count", [0, 3, -1, True, 1.0, "1"])
def test_click_point_rejects_bad_click_count(patch_win32, count):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.click_point(960, 540, click_count=count)

    assert ok is False
    assert reason == "invalid_click_count"
    assert fake.sendinput_batches == []


@pytest.mark.parametrize("point", [("a", 5), (5, None), (True, 5)])
def test_click_point_rejects_non_int_coordinates(patch_win32, point):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.click_point(point[0], point[1])

    assert ok is False
    assert reason == "invalid_point"
    assert fake.sendinput_batches == []


def test_click_point_cursor_never_lands_sends_no_button(patch_win32):
    fake, _t = patch_win32(FakeUser32(cursor_lands=False))

    ok, reason = wis.click_point(960, 540)

    assert ok is False
    assert reason == "cursor_did_not_land"
    assert fake.button_batches() == []
    # One MOVE per outer attempt, and nothing else.
    assert len(fake.move_batches()) == wis._CLICK_MOVE_ATTEMPTS


def test_click_point_short_button_batch_releases_and_reports(patch_win32):
    # The MOVE batch is index 0; the button batch is index 1 and only its
    # first event (the DOWN) is accepted. The primitive must send a
    # compensating UP so the button is not left held, and report the short
    # send.
    fake, _t = patch_win32(FakeUser32(send_returns=[None, 1]))

    ok, reason = wis.click_point(960, 540)

    assert ok is False
    assert reason == "sendinput_short"
    buttons = fake.button_batches()
    assert len(buttons) == 2
    assert len(buttons[1]) == 1
    assert buttons[1][0]["flags"] & wis.MOUSEEVENTF_LEFTUP


def test_click_point_reports_release_failed_when_compensation_fails(
    patch_win32,
):
    # wh-mouse-grid.1.5: the button batch half-delivers (DOWN accepted, UP
    # refused) AND the compensating release is refused too. The button may
    # still be held, and sendinput_short would hide that -- release_failed
    # is the one reason that tells the caller so.
    fake, _t = patch_win32(FakeUser32(send_returns=[None, 1, 0]))

    ok, reason = wis.click_point(960, 540)

    assert ok is False
    assert reason == "release_failed"


def test_click_point_zero_accepted_sends_no_compensating_up(patch_win32):
    # Nothing was injected, so the button was never pressed; a spurious UP
    # could register as a real release with nothing down.
    fake, _t = patch_win32(FakeUser32(send_returns=[None, 0]))

    ok, reason = wis.click_point(960, 540)

    assert (ok, reason) == (False, "sendinput_short")
    assert len(fake.button_batches()) == 1


def test_click_point_sendinput_raise_fails_soft(patch_win32):
    fake, _t = patch_win32(FakeUser32(raise_on_batch=1))

    ok, reason = wis.click_point(960, 540)

    assert ok is False
    assert reason == "sendinput_error"


def test_click_point_blocks_and_releases_physical_input(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    wis.click_point(960, 540)

    assert fake.blockinput_calls == [1, 0]


def test_click_point_releases_block_when_sendinput_raises(patch_win32):
    fake, _t = patch_win32(FakeUser32(raise_on_batch=1))

    wis.click_point(960, 540)

    assert fake.blockinput_calls == [1, 0]


def test_click_point_negative_virtual_desktop_origin(patch_win32):
    # A monitor left of and above the primary gives the virtual desktop a
    # negative origin. A physical point ON that monitor is negative too and
    # must still normalize into 0..65535 relative to the virtual-desktop box.
    metrics = {
        wis.SM_XVIRTUALSCREEN: -1920,
        wis.SM_YVIRTUALSCREEN: -1080,
        wis.SM_CXVIRTUALSCREEN: 3841,   # -1920 .. 1920 inclusive
        wis.SM_CYVIRTUALSCREEN: 2161,   # -1080 .. 1080 inclusive
    }
    fake, _t = patch_win32(FakeUser32(metrics=metrics))

    ok, reason = wis.click_point(-1920, -1080)

    assert (ok, reason) == (True, None)
    move = fake.move_batches()[0][0]
    # The virtual desktop's top-left corner is exactly 0 on both axes.
    assert move["dx"] == 0 and move["dy"] == 0


def test_click_point_negative_origin_far_corner_is_max(patch_win32):
    metrics = {
        wis.SM_XVIRTUALSCREEN: -1920,
        wis.SM_YVIRTUALSCREEN: -1080,
        wis.SM_CXVIRTUALSCREEN: 3841,
        wis.SM_CYVIRTUALSCREEN: 2161,
    }
    fake, _t = patch_win32(FakeUser32(metrics=metrics))

    ok, _reason = wis.click_point(1920, 1080)

    assert ok is True
    move = fake.move_batches()[0][0]
    assert move["dx"] == 65535 and move["dy"] == 65535


# ---------------------------------------------------------------------------
# move_pointer_to
# ---------------------------------------------------------------------------


def test_move_pointer_to_sends_one_move_and_no_buttons(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.move_pointer_to(100, 200)

    assert (ok, reason) == (True, None)
    assert len(fake.sendinput_batches) == 1
    assert fake.button_batches() == []
    move = fake.sendinput_batches[0][0]
    assert move["flags"] == (
        wis.MOUSEEVENTF_MOVE
        | wis.MOUSEEVENTF_ABSOLUTE
        | wis.MOUSEEVENTF_VIRTUALDESK
    )


def test_move_pointer_to_reports_a_cursor_that_never_lands(patch_win32):
    fake, _t = patch_win32(FakeUser32(cursor_lands=False))

    ok, reason = wis.move_pointer_to(100, 200)

    assert (ok, reason) == (False, "cursor_did_not_land")
    assert fake.button_batches() == []


def test_move_pointer_to_rejects_non_int_coordinates(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.move_pointer_to(1.5, 200)

    assert (ok, reason) == (False, "invalid_point")
    assert fake.sendinput_batches == []


def test_move_pointer_to_sendinput_raise_fails_soft(patch_win32):
    fake, _t = patch_win32(FakeUser32(raise_on_batch=0))

    ok, reason = wis.move_pointer_to(100, 200)

    assert (ok, reason) == (False, "sendinput_error")


def test_move_pointer_to_retries_the_move_when_the_verify_lags(patch_win32):
    # The first MOVE is posted but never observed by GetCursorPos (the real
    # SendInput/GetCursorPos race); a re-sent MOVE lands. The hover must
    # retry like the other two grid primitives instead of failing outright
    # (wh-mouse-grid.1.2).
    fake, _t = patch_win32(LandsOnSecondMoveUser32())

    ok, reason = wis.move_pointer_to(100, 200)

    assert (ok, reason) == (True, None)
    assert len(fake.move_batches()) == 2
    assert fake.button_batches() == []


def test_move_pointer_to_exhausts_all_move_attempts_before_failing(
    patch_win32,
):
    fake, _t = patch_win32(FakeUser32(cursor_lands=False))

    ok, reason = wis.move_pointer_to(100, 200)

    assert (ok, reason) == (False, "cursor_did_not_land")
    assert len(fake.move_batches()) == wis._CLICK_MOVE_ATTEMPTS


# ---------------------------------------------------------------------------
# The pure drag geometry helpers
# ---------------------------------------------------------------------------


def test_drag_step_count_scales_with_duration_and_is_bounded():
    # duration 0 is the documented no-interpolation choice (click_config.py):
    # a single move then release (wh-mouse-grid.1.10).
    assert wis._drag_step_count(0) == 1
    assert wis._drag_step_count(1) == wis._DRAG_MIN_STEPS
    assert wis._drag_step_count(250) > wis._DRAG_MIN_STEPS
    assert wis._drag_step_count(250) <= wis._DRAG_MAX_STEPS
    assert wis._drag_step_count(10_000) == wis._DRAG_MAX_STEPS


def test_interpolated_points_end_exactly_on_the_target():
    points = wis._interpolate_drag_points(0, 0, 100, 50, 10)

    assert len(points) == 10
    assert points[-1] == (100, 50)
    # Strictly advancing toward the end, never jumping past it.
    xs = [p[0] for p in points]
    assert xs == sorted(xs)
    assert all(0 <= x <= 100 for x in xs)


def test_interpolated_points_handle_a_negative_direction():
    points = wis._interpolate_drag_points(500, 400, -100, -200, 8)

    assert points[-1] == (-100, -200)
    xs = [p[0] for p in points]
    assert xs == sorted(xs, reverse=True)


# ---------------------------------------------------------------------------
# drag_pointer
# ---------------------------------------------------------------------------


def test_drag_presses_moves_gradually_then_releases(patch_win32):
    fake, fake_time = patch_win32(FakeUser32())

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (True, None)

    flat = [e for batch in fake.sendinput_batches for e in batch]
    downs = [e for e in flat if e["flags"] & wis.MOUSEEVENTF_LEFTDOWN]
    ups = [e for e in flat if e["flags"] & wis.MOUSEEVENTF_LEFTUP]
    moves = [e for e in flat if e["flags"] & wis.MOUSEEVENTF_MOVE]
    assert len(downs) == 1
    assert len(ups) == 1
    # One MOVE to the start point plus one per interpolation step.
    steps = wis._drag_step_count(250)
    assert len(moves) == 1 + steps
    # The movement was gradual: it slept between steps.
    assert len(fake_time.sleeps) == steps
    assert sum(fake_time.sleeps) == pytest.approx(0.25, rel=1e-6)

    # Order: the DOWN precedes every interpolation move, and the UP is last.
    order = [
        "down" if e["flags"] & wis.MOUSEEVENTF_LEFTDOWN
        else "up" if e["flags"] & wis.MOUSEEVENTF_LEFTUP
        else "move"
        for e in flat
    ]
    assert order[0] == "move"          # the verified move to the start point
    assert order[1] == "down"
    assert order[-1] == "up"
    assert "down" not in order[2:]


def test_drag_never_teleports_the_pointer_to_the_end(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    wis.drag_pointer(0, 0, 1000, 0, duration_ms=250)

    move_xs = [
        e["dx"] for batch in fake.sendinput_batches for e in batch
        if e["flags"] & wis.MOUSEEVENTF_MOVE
    ]
    # More than a start and an end: the intermediate points are what makes an
    # application recognise the gesture as a drag.
    assert len(move_xs) >= 4
    assert move_xs == sorted(move_xs)


def test_drag_releases_the_button_when_a_move_raises_mid_movement(
    patch_win32,
):
    # Batch 0 is the verified move to the start, batch 1 the LEFTDOWN, so
    # batch 4 is the third interpolation move. SendInput raising there is the
    # simulated mid-movement failure: the release MUST still happen.
    fake, _t = patch_win32(FakeUser32(raise_on_batch=4))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert ok is False
    assert reason == "sendinput_error"

    flat = [e for batch in fake.sendinput_batches for e in batch]
    ups = [e for e in flat if e["flags"] & wis.MOUSEEVENTF_LEFTUP]
    assert len(ups) == 1, "the button must be released on the failure path"
    # The release is the last thing sent.
    assert flat[-1]["flags"] & wis.MOUSEEVENTF_LEFTUP
    # It really did fail partway: far fewer moves than a whole drag.
    moves = [e for e in flat if e["flags"] & wis.MOUSEEVENTF_MOVE]
    assert len(moves) < 1 + wis._drag_step_count(250)


def test_drag_releases_the_button_when_a_move_short_sends(patch_win32):
    # The third interpolation move (batch index 4) is refused outright.
    returns: list[Any] = [None] * 4 + [0]
    fake, _t = patch_win32(FakeUser32(send_returns=returns))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert ok is False
    assert reason == "sendinput_short"
    flat = [e for batch in fake.sendinput_batches for e in batch]
    assert flat[-1]["flags"] & wis.MOUSEEVENTF_LEFTUP


def test_drag_reports_a_failed_release(patch_win32):
    # The final MOVE+UP pair only half-delivers (the UP is refused) and the
    # compensating in-place release is refused too. The button is now stuck
    # down, and the caller must be told.
    steps = wis._drag_step_count(250)
    returns: list[Any] = [None] * (1 + steps) + [1, 0]
    fake, _t = patch_win32(FakeUser32(send_returns=returns))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert ok is False
    assert reason == "release_failed"


def test_drag_sends_the_final_move_and_release_as_one_batch(patch_win32):
    # wh-mouse-grid.1.4: the interpolated movement runs with physical input
    # unblocked, so a physical mouse movement between the last injected MOVE
    # and a separate UP would relocate the drop. SendInput injects a batch
    # contiguously, so the final MOVE and the UP must travel together, with
    # the UP carrying the end coordinates itself.
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (True, None)
    final = fake.sendinput_batches[-1]
    assert len(final) == 2
    move, up = final
    assert move["flags"] & wis.MOUSEEVENTF_MOVE
    assert up["flags"] & wis.MOUSEEVENTF_LEFTUP
    assert up["flags"] & wis.MOUSEEVENTF_ABSOLUTE
    end_nx, end_ny = wis._normalize_to_virtual_desktop(500, 300)
    assert (move["dx"], move["dy"]) == (end_nx, end_ny)
    assert (up["dx"], up["dy"]) == (end_nx, end_ny)
    # No other batch carries the UP -- the atomic pair is the only release.
    ups = [
        e for batch in fake.sendinput_batches for e in batch
        if e["flags"] & wis.MOUSEEVENTF_LEFTUP
    ]
    assert len(ups) == 1


def test_drag_partial_final_pair_falls_back_to_in_place_release(patch_win32):
    # The final MOVE+UP pair delivers only its MOVE (accepted == 1), so the
    # button is still held at the end point. The guaranteed in-place release
    # must still run, and its success keeps the reason at sendinput_short --
    # the button is NOT left held.
    steps = wis._drag_step_count(250)
    returns: list[Any] = [None] * (1 + steps) + [1]
    fake, _t = patch_win32(FakeUser32(send_returns=returns))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "sendinput_short")
    last = fake.sendinput_batches[-1]
    assert len(last) == 1
    assert last[0]["flags"] & wis.MOUSEEVENTF_LEFTUP
    # The in-place release deliberately carries no ABSOLUTE coordinates.
    assert not (last[0]["flags"] & wis.MOUSEEVENTF_ABSOLUTE)


def test_drag_mid_movement_raise_with_failed_cleanup_reports_release_failed(
    patch_win32,
):
    # wh-mouse-grid.1.9: a movement SendInput raises AND the cleanup in-place
    # release is refused. The stuck button outranks the movement error: the
    # caller must see release_failed, not the generic sendinput_error that
    # Logic would announce as a plain grid_drag_failed while the button is
    # still held.
    # Batch 0 = start MOVE, 1 = LEFTDOWN, 4 = third interpolation move
    # (raises), 5 = the cleanup in-place LEFTUP (refused).
    returns: list[Any] = [None] * 5 + [0]
    fake, _t = patch_win32(FakeUser32(send_returns=returns, raise_on_batch=4))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "release_failed")
    last = fake.sendinput_batches[-1]
    assert len(last) == 1
    assert last[0]["flags"] & wis.MOUSEEVENTF_LEFTUP


def test_drag_duration_zero_sends_one_move_then_the_release(patch_win32):
    # wh-mouse-grid.1.10: drag_duration_ms=0 is documented as the
    # no-interpolation choice -- a single move then release. After the start
    # MOVE and the LEFTDOWN there must be exactly ONE further batch: the
    # atomic final [MOVE, UP] pair at the end point. Eight zero-delay
    # interpolated moves would break the documented contract, not just its
    # timing.
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=0)

    assert (ok, reason) == (True, None)
    # Batches: start MOVE, LEFTDOWN, final [MOVE, UP]. Nothing else.
    assert len(fake.sendinput_batches) == 3
    final = fake.sendinput_batches[-1]
    assert len(final) == 2
    end_nx, end_ny = wis._normalize_to_virtual_desktop(500, 300)
    assert (final[0]["dx"], final[0]["dy"]) == (end_nx, end_ny)
    assert final[1]["flags"] & wis.MOUSEEVENTF_LEFTUP


def test_drag_does_not_press_when_the_start_move_never_lands(patch_win32):
    fake, _t = patch_win32(FakeUser32(cursor_lands=False))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "cursor_did_not_land")
    flat = [e for batch in fake.sendinput_batches for e in batch]
    assert not [e for e in flat if e["flags"] & wis.MOUSEEVENTF_LEFTDOWN]
    assert not [e for e in flat if e["flags"] & wis.MOUSEEVENTF_LEFTUP]


def test_drag_refused_button_down_sends_no_release(patch_win32):
    # The LEFTDOWN batch (index 1) is refused: nothing was pressed, so no
    # release must be synthesised.
    fake, _t = patch_win32(FakeUser32(send_returns=[None, 0]))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "sendinput_short")
    flat = [e for batch in fake.sendinput_batches for e in batch]
    assert not [e for e in flat if e["flags"] & wis.MOUSEEVENTF_LEFTUP]


@pytest.mark.parametrize("duration", [-1, 60_001, "250", 250.0, True, None])
def test_drag_rejects_a_bad_duration(patch_win32, duration):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.drag_pointer(0, 0, 10, 10, duration_ms=duration)

    assert (ok, reason) == (False, "invalid_duration")
    assert fake.sendinput_batches == []


def test_drag_rejects_non_int_coordinates(patch_win32):
    fake, _t = patch_win32(FakeUser32())

    ok, reason = wis.drag_pointer(0, 0, "10", 10, duration_ms=250)

    assert (ok, reason) == (False, "invalid_point")
    assert fake.sendinput_batches == []


# ---------------------------------------------------------------------------
# drag_pointer BlockInput (wh-mouse-grid.1.1): physical input is suppressed
# for the initial move-and-press only, and always released -- matching
# click_point. The interpolated movement runs unblocked on purpose (a drag
# can take hundreds of milliseconds; each step re-asserts absolute
# coordinates, so contention mid-drag cannot change where it ends).
# ---------------------------------------------------------------------------


def test_drag_blocks_physical_input_for_move_and_press_only(patch_win32):
    fake, _t = patch_win32(OrderRecordingUser32())

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (True, None)
    assert fake.blockinput_calls == [1, 0]
    # The block opens before anything is sent and closes right after the
    # button-down, before the first interpolation move.
    assert fake.ops[0] == "block:1"
    down_index = fake.ops.index("send:down")
    assert fake.ops[down_index + 1] == "block:0"


def test_drag_releases_block_when_the_start_move_never_lands(patch_win32):
    fake, _t = patch_win32(FakeUser32(cursor_lands=False))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "cursor_did_not_land")
    assert fake.blockinput_calls == [1, 0]


def test_drag_releases_block_when_the_button_down_is_refused(patch_win32):
    fake, _t = patch_win32(FakeUser32(send_returns=[None, 0]))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "sendinput_short")
    assert fake.blockinput_calls == [1, 0]


def test_drag_releases_block_when_the_down_raises(patch_win32):
    # Batch 1 is the LEFTDOWN; SendInput raising there must still release
    # the physical-input block on the way out.
    fake, _t = patch_win32(FakeUser32(raise_on_batch=1))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "sendinput_error")
    assert fake.blockinput_calls == [1, 0]


def test_drag_mid_movement_failure_does_not_re_block(patch_win32):
    # A failure after the block was already released (mid-interpolation)
    # must not touch BlockInput again.
    fake, _t = patch_win32(FakeUser32(raise_on_batch=4))

    ok, reason = wis.drag_pointer(100, 100, 500, 300, duration_ms=250)

    assert (ok, reason) == (False, "sendinput_error")
    assert fake.blockinput_calls == [1, 0]
