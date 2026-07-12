"""Unit tests for the SendInput-backed coordinate-click primitive.

``utils.win_input_sender.click_at`` is the production coordinate-click seam
injected into ``ClickExecutor`` (wh-l4h.1). A click at the wrong coordinate is
the exact hands-free hazard the executor's fallback exists to prevent, so the
primitive is fail-closed: it normalizes the physical pixel to ABSOLUTE
virtual-desktop units, sends a MOVE, then VERIFIES the cursor landed via
GetCursorPos before synthesising any button event, and counts only the
LEFTDOWN/LEFTUP pair so the executor's short-send check works.

These tests fake ``user32`` (SendInput / GetSystemMetrics / GetCursorPos) so
they run headless with no real input synthesis. They assert the normalization
math, the MOUSEEVENTF flags on the captured Input structs, the cursor-verify
fail-closed path, the short-send semantics, and the SendInput-raises fail-soft
path.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any, Optional

import pytest

from utils import win_input_sender as wis


# Known virtual-desktop box used by the happy-path tests: origin (0, 0),
# size (1921, 1081) so the span-1 divisor is a clean 1920 / 1080. A physical
# point of (960, 540) then normalizes to 960*65535/1920 = 32767.5 -> 32768
# (round-half-up) on X and 540*65535/1080 = 32767.5 -> 32768 on Y.
VS_ORIGIN_X = 0
VS_ORIGIN_Y = 0
VS_WIDTH = 1921
VS_HEIGHT = 1081


class FakeUser32:
    """Records SendInput calls and serves scripted GetSystemMetrics/GetCursorPos.

    SendInput is recorded as (num_events, [(type, dx, dy, dwFlags), ...]) by
    reading the passed ctypes Input array via ``byref``'s underlying object.
    The fake matches the real call shape: ``SendInput(num, byref(arr), sizeof)``.
    """

    def __init__(
        self,
        *,
        cursor: tuple[int, int],
        cursor_ok: bool = True,
        click_return: int = 2,
        metrics: Optional[dict[int, int]] = None,
        sendinput_raises_on_click: bool = False,
        cursor_sequence: Optional[list[tuple[int, int]]] = None,
        cursor_ok_sequence: Optional[list[bool]] = None,
        up_return: Optional[int] = None,
        blockinput_return: int = 1,
    ) -> None:
        self._cursor = cursor
        self._cursor_ok = cursor_ok
        self._click_return = click_return
        self._sendinput_raises_on_click = sendinput_raises_on_click
        self._blockinput_return = blockinput_return
        # Optional scripted GetCursorPos success flags: the API returns the next
        # entry per call and holds the last once exhausted. Drives the
        # transient-API-failure retry test (wh-review-click-overlay-glm52.2).
        self._cursor_ok_sequence = cursor_ok_sequence
        # Optional return value for the compensating LEFTUP (a 1-event LEFTUP
        # batch). None means the up succeeds; 0 makes it fail so a test can drive
        # the stuck-button-logging path (wh-review-click-overlay-glm52.1). The
        # 1-event MOVE batch is unaffected -- it is told apart by its flags.
        self._up_return = up_return
        # Ordered record of BlockInput(flag) calls: 1 = block, 0 = release.
        self.blockinput_calls: list[int] = []
        # Ordered log of BlockInput / SendInput calls so a test can assert the
        # click batch fires WHILE physical input is blocked, and that BlockInput
        # is always released (wh-click-mouse-contention).
        self.call_log: list[tuple[str, int]] = []
        # Optional scripted cursor positions: GetCursorPos returns the next
        # entry on each call and holds the last entry once exhausted. This lets
        # a test drive the post-MOVE bounded-retry verify (wh-9f3t.76.1) -- e.g.
        # a stale far read followed by a landed read.
        self._cursor_sequence = cursor_sequence
        self.getcursorpos_calls = 0
        self._metrics = metrics or {
            wis.SM_XVIRTUALSCREEN: VS_ORIGIN_X,
            wis.SM_YVIRTUALSCREEN: VS_ORIGIN_Y,
            wis.SM_CXVIRTUALSCREEN: VS_WIDTH,
            wis.SM_CYVIRTUALSCREEN: VS_HEIGHT,
        }
        # Captured SendInput batches: list of list-of-dicts (one per event).
        self.sendinput_batches: list[list[dict[str, int]]] = []

    def GetSystemMetrics(self, index: int) -> int:
        return self._metrics[index]

    def GetCursorPos(self, lp_point: Any) -> int:
        # lp_point is ctypes.byref(POINT); resolve to the POINT object.
        point = lp_point._obj  # type: ignore[attr-defined]
        if self._cursor_sequence:
            idx = min(self.getcursorpos_calls, len(self._cursor_sequence) - 1)
            cx, cy = self._cursor_sequence[idx]
        else:
            cx, cy = self._cursor
        point.x = cx
        point.y = cy
        idx = self.getcursorpos_calls
        self.getcursorpos_calls += 1
        if self._cursor_ok_sequence:
            ok = self._cursor_ok_sequence[
                min(idx, len(self._cursor_ok_sequence) - 1)
            ]
            return 1 if ok else 0
        return 1 if self._cursor_ok else 0

    def BlockInput(self, flag: int) -> int:
        self.blockinput_calls.append(int(flag))
        self.call_log.append(("block", int(flag)))
        return self._blockinput_return

    def SendInput(self, num: int, lp_array: Any, _size: int) -> int:
        self.call_log.append(("send", int(num)))
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
        # A 2-event batch is the click; a 1-event batch is either the MOVE or
        # the compensating LEFTUP, told apart by its flags.
        is_click_batch = num == 2
        if is_click_batch and self._sendinput_raises_on_click:
            raise OSError("SendInput failed at the platform boundary")
        if is_click_batch:
            return self._click_return
        # A scripted return for the compensating LEFTUP only (not the MOVE).
        if (
            self._up_return is not None
            and num == 1
            and (batch[0]["flags"] & wis.MOUSEEVENTF_LEFTUP)
        ):
            return self._up_return
        return num  # MOVE batch accepts its single event


@pytest.fixture
def patch_user32(monkeypatch):
    """Install a FakeUser32 onto the module and return a setter."""

    def _install(fake: FakeUser32) -> FakeUser32:
        monkeypatch.setattr(wis, "user32", fake)
        # kernel32.GetLastError is only used on the error log path; stub it so
        # the logging branches do not touch the real Win32.
        monkeypatch.setattr(
            wis, "kernel32", type("K", (), {"GetLastError": staticmethod(lambda: 0)})()
        )
        return fake

    return _install


def test_happy_path_normalizes_flags_and_returns_true_2(patch_user32):
    fake = patch_user32(
        FakeUser32(cursor=(960, 540), cursor_ok=True, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    # Two SendInput batches: the MOVE (1 event) then the click (2 events).
    assert len(fake.sendinput_batches) == 2
    move_batch, click_batch = fake.sendinput_batches
    assert len(move_batch) == 1
    assert len(click_batch) == 2

    # Normalization: (960, 540) over a 1920x1080 span maps to (32768, 32768)
    # with round-half-up.
    move = move_batch[0]
    assert move["type"] == wis.INPUT_MOUSE
    assert move["dx"] == 32768
    assert move["dy"] == 32768
    assert move["flags"] == (
        wis.MOUSEEVENTF_MOVE
        | wis.MOUSEEVENTF_ABSOLUTE
        | wis.MOUSEEVENTF_VIRTUALDESK
    )

    down, up = click_batch
    assert down["dx"] == 32768 and down["dy"] == 32768
    assert up["dx"] == 32768 and up["dy"] == 32768
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


def test_cursor_did_not_land_fails_closed_no_click(patch_user32):
    # GetCursorPos reports a far-away point on every poll of every attempt ->
    # fail closed: no click batch (only a MOVE batch per outer move attempt).
    fake = patch_user32(
        FakeUser32(cursor=(50, 60), cursor_ok=True, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0
    # One MOVE batch per outer attempt; the click batch was never sent.
    assert len(fake.sendinput_batches) == wis._CLICK_MOVE_ATTEMPTS
    assert all(len(batch) == 1 for batch in fake.sendinput_batches)


def test_stale_first_read_then_landed_retries_then_clicks(patch_user32):
    # wh-9f3t.76.1: the MOVE is delivered asynchronously, so the first
    # GetCursorPos can read the stale pre-move position. The verify must retry
    # and accept the later landed read. First read is far (50, 60); second read
    # is on target (960, 540). The click must succeed AND only fire after the
    # landing read (so GetCursorPos is called more than once before the click
    # batch is sent).
    fake = patch_user32(
        FakeUser32(
            cursor=(0, 0),
            cursor_ok=True,
            click_return=2,
            cursor_sequence=[(50, 60), (960, 540)],
        )
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    # GetCursorPos was polled more than once: the stale read did not fail closed.
    assert fake.getcursorpos_calls == 2
    # Two batches: the MOVE, then the click sent only after the landing read.
    assert len(fake.sendinput_batches) == 2
    assert len(fake.sendinput_batches[0]) == 1   # MOVE
    assert len(fake.sendinput_batches[1]) == 2   # LEFTDOWN/LEFTUP


def test_exhausted_stale_reads_fail_closed_polls_every_attempt(patch_user32):
    # Every read stays far from the target -> the verify polls of every move
    # attempt are exhausted, fail closed with no click. GetCursorPos was polled
    # the full budget (verify polls x move attempts), proving both loops ran.
    fake = patch_user32(
        FakeUser32(cursor=(50, 60), cursor_ok=True, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0
    assert fake.getcursorpos_calls == (
        wis._CLICK_CURSOR_VERIFY_ATTEMPTS * wis._CLICK_MOVE_ATTEMPTS
    )
    # One MOVE batch per outer attempt, no click batch.
    assert len(fake.sendinput_batches) == wis._CLICK_MOVE_ATTEMPTS


def test_cursor_within_tolerance_still_clicks(patch_user32):
    # Cursor landed 2px off on each axis -- within tolerance -> still clicks.
    fake = patch_user32(
        FakeUser32(cursor=(962, 542), cursor_ok=True, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    assert len(fake.sendinput_batches) == 2


def test_getcursorpos_failure_fails_closed(patch_user32):
    # GetCursorPos returns 0 on every poll of every attempt (the read itself
    # keeps failing) -> fail closed, no click. The API failure is now treated
    # like a positioning miss: each of the _CLICK_MOVE_ATTEMPTS attempts sends
    # its MOVE and then breaks on the failed read, so there are as many MOVE
    # batches as attempts and never a click batch (wh-review-click-overlay-glm52.2).
    fake = patch_user32(
        FakeUser32(cursor=(960, 540), cursor_ok=False, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0
    # One MOVE per attempt, no click batch.
    assert len(fake.sendinput_batches) == wis._CLICK_MOVE_ATTEMPTS
    assert all(len(b) == 1 for b in fake.sendinput_batches)


def test_getcursorpos_transient_failure_retries_then_succeeds(patch_user32):
    # A GetCursorPos API failure on the first attempt must NOT abort the whole
    # click. The outer move-retry loop tries again; when GetCursorPos succeeds on
    # the second attempt with the cursor on target, the click goes through
    # (wh-review-click-overlay-glm52.2). Fail-closed is preserved: no click batch
    # is sent on the failed attempt.
    fake = patch_user32(
        FakeUser32(
            cursor=(960, 540),
            cursor_ok=True,
            cursor_ok_sequence=[False, True],  # fail attempt 1's poll, then ok
        )
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    # Attempt 1: MOVE only (read failed -> no click). Attempt 2: MOVE + click.
    assert len(fake.sendinput_batches) == 3
    assert len(fake.sendinput_batches[0]) == 1  # attempt 1 MOVE
    assert len(fake.sendinput_batches[1]) == 1  # attempt 2 MOVE
    assert len(fake.sendinput_batches[2]) == 2  # attempt 2 click batch
    assert fake.getcursorpos_calls >= 2


def test_compensating_up_failure_is_logged(patch_user32, caplog):
    # The click batch accepts only the LEFTDOWN (events_sent == 1) AND the
    # compensating LEFTUP is also refused (returns 0). The button is now stuck
    # down; click_at must log an error naming the stuck-button hazard so the
    # failure is diagnosable (wh-review-click-overlay-glm52.1). It still reports
    # (False, 1).
    fake = patch_user32(
        FakeUser32(
            cursor=(960, 540), cursor_ok=True, click_return=1, up_return=0
        )
    )

    with caplog.at_level(logging.ERROR, logger="utils.win_input_sender"):
        success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 1
    # The compensating LEFTUP was still attempted (MOVE, click, up).
    assert len(fake.sendinput_batches) == 3
    text = caplog.text.lower()
    assert "compensating" in text and "stuck" in text


def test_short_click_send_is_success_false_events_1(patch_user32):
    # The click batch only accepts 1 of 2 events. That first event is the
    # LEFTDOWN, so the logical left button is now held down; click_at must send a
    # compensating LEFTUP before returning so a partial batch cannot leave the
    # button stuck (a drag/selection hazard -- wh-review-click-overlay-codex.1).
    # It still reports (False, 1) so the executor maps it to sendinput_short.
    fake = patch_user32(
        FakeUser32(cursor=(960, 540), cursor_ok=True, click_return=1)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 1
    # MOVE, the short click batch, THEN a compensating LEFTUP.
    assert len(fake.sendinput_batches) == 3
    comp = fake.sendinput_batches[2]
    assert len(comp) == 1
    assert comp[0]["flags"] == (
        wis.MOUSEEVENTF_LEFTUP
        | wis.MOUSEEVENTF_ABSOLUTE
        | wis.MOUSEEVENTF_VIRTUALDESK
    )
    # The compensating up carries the same verified coordinates as the click.
    assert comp[0]["dx"] == 32768 and comp[0]["dy"] == 32768


def test_zero_click_send_sends_no_compensating_up(patch_user32):
    # The click batch accepts 0 events -> nothing was injected and the button was
    # never pressed, so click_at must NOT send a spurious LEFTUP (which could
    # register as a real release with nothing down). Reports (False, 0).
    fake = patch_user32(
        FakeUser32(cursor=(960, 540), cursor_ok=True, click_return=0)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0
    # MOVE then the (empty) click batch only -- no third compensating batch.
    assert len(fake.sendinput_batches) == 2


def test_sendinput_raises_fails_soft(patch_user32):
    # SendInput raises on the click batch -> (False, 0), never propagates.
    patch_user32(
        FakeUser32(
            cursor=(960, 540),
            cursor_ok=True,
            sendinput_raises_on_click=True,
        )
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0


def test_degenerate_virtual_desktop_pins_axis_to_zero(patch_user32):
    # A degenerate (span <= 1) virtual-desktop axis cannot be normalized; that
    # axis pins to 0 rather than dividing by zero. The cursor still verifies
    # here (so we can read the MOVE coords), proving the guard, not a crash.
    metrics = {
        wis.SM_XVIRTUALSCREEN: 0,
        wis.SM_YVIRTUALSCREEN: 0,
        wis.SM_CXVIRTUALSCREEN: 1,   # degenerate width
        wis.SM_CYVIRTUALSCREEN: 1081,
    }
    fake = patch_user32(
        FakeUser32(cursor=(960, 540), cursor_ok=True, metrics=metrics)
    )

    success, _events = wis.click_at(960, 540)

    assert success is True
    move = fake.sendinput_batches[0][0]
    assert move["dx"] == 0           # degenerate X pinned to 0
    assert move["dy"] == 32768       # Y normalized normally


# --- Physical-input suppression around the click (wh-click-mouse-contention) ---
# When the physical mouse is being moved, its motion overrides the injected
# absolute MOVE for the whole verify window, so click_at reads the physical
# position and fails closed even though the target is correct. click_at blocks
# physical input for the brief move+verify+click and retries the whole block a
# few times as a backup. The on-target verification and fail-closed semantics
# are unchanged.


def test_blockinput_wraps_move_and_click_and_releases(patch_user32):
    # Physical input is blocked before the MOVE and released after the click,
    # and the click batch fires WHILE still blocked (order proves it).
    fake = patch_user32(
        FakeUser32(cursor=(960, 540), cursor_ok=True, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    assert fake.call_log == [
        ("block", 1),   # block physical input first
        ("send", 1),    # MOVE
        ("send", 2),    # LEFTDOWN/LEFTUP, still blocked
        ("block", 0),   # release
    ]


def test_blockinput_released_when_cursor_never_lands(patch_user32):
    # Every verify poll of every move attempt misses -> fail closed with no
    # click, and BlockInput is released each attempt (physical input is never
    # left stuck).
    fake = patch_user32(
        FakeUser32(cursor=(50, 60), cursor_ok=True, click_return=2)
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0
    # One balanced block/release pair per outer attempt, ending on a release.
    assert fake.blockinput_calls == [1, 0] * wis._CLICK_MOVE_ATTEMPTS
    # No click batch ever went out (only MOVE batches).
    assert all(len(batch) == 1 for batch in fake.sendinput_batches)


def test_blockinput_released_when_sendinput_raises(patch_user32):
    # SendInput raises on the click batch -> (False, 0) and BlockInput is still
    # released by the finally, so physical input is never left blocked.
    fake = patch_user32(
        FakeUser32(
            cursor=(960, 540),
            cursor_ok=True,
            sendinput_raises_on_click=True,
        )
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is False
    assert events_sent == 0
    assert fake.blockinput_calls == [1, 0]


def test_outer_retry_relands_after_first_attempt_misses(patch_user32):
    # The first move attempt's verify polls all miss (physical mouse briefly
    # overriding the move); the whole move is retried and the next attempt
    # lands, then the click succeeds.
    misses = [(50, 60)] * wis._CLICK_CURSOR_VERIFY_ATTEMPTS
    fake = patch_user32(
        FakeUser32(
            cursor=(0, 0),
            cursor_ok=True,
            click_return=2,
            cursor_sequence=misses + [(960, 540)],
        )
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    # First attempt polled the full budget and missed; second attempt landed on
    # its first poll.
    assert fake.getcursorpos_calls == wis._CLICK_CURSOR_VERIFY_ATTEMPTS + 1
    # Two outer attempts => two block/release pairs.
    assert fake.blockinput_calls == [1, 0, 1, 0]


def test_blockinput_ignored_still_clicks_best_effort(patch_user32):
    # If BlockInput returns 0 (another thread already blocked, or no privilege),
    # the click still proceeds best-effort and no release is issued (we never
    # successfully blocked).
    fake = patch_user32(
        FakeUser32(
            cursor=(960, 540),
            cursor_ok=True,
            click_return=2,
            blockinput_return=0,
        )
    )

    success, events_sent = wis.click_at(960, 540)

    assert success is True
    assert events_sent == 2
    assert fake.blockinput_calls == [1]            # attempted once, no release
    assert fake.call_log == [("block", 1), ("send", 1), ("send", 2)]
