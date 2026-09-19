"""Tests for the click gesture parameter (wh-click-gesture-param).

The gesture parameter carries "right click" / "double click" through the
existing clicking subsystem: ``ElementQuery`` gains a ``gesture`` field whose
DEFAULT is today's Invoke behaviour, and ``ClickExecutor`` routes a non-default
gesture to the EXISTING guarded coordinate-click path (full five-step pre-click
verification plus both occlusion hit-test layers) with a different mouse button
or click count.

Authoritative spec: docs/superpowers/specs/2026-08-09-mouse-grid-overlay-design.md
("Gestures on named and numbered controls" + the "Gesture parameter" subsection
under Architecture).

Load-bearing invariant asserted throughout: a non-default gesture NEVER calls
Invoke (Invoke has no concept of a button or a count), and every guard the
default path applies still applies -- an obstructed click point, a failed
re-verification, or an ineligible match refuses with NO input sent.
"""

from __future__ import annotations

from typing import Any, Optional

from ui.click_executor import (
    ClickExecutor,
    ForegroundProbe,
    SnapshotForeground,
)
from ui.element_types import ClickGesture, ElementMatch, ElementQuery


# ---------------------------------------------------------------------------
# Fakes (same shapes as tests/test_click_executor.py, kept local so this file
# runs standalone).
# ---------------------------------------------------------------------------

class FakeRect:
    """tagRECT-like object: left/top/right/bottom (UIA's native shape)."""

    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeControl:
    """A fake control_ref exposing the executor's Current* + Invoke surface."""

    def __init__(self, *, is_enabled: bool = True) -> None:
        self._is_enabled = is_enabled
        self._rect = FakeRect(100, 100, 140, 130)
        self.invoke_calls = 0
        self.dda_calls = 0

    @property
    def CurrentIsEnabled(self) -> bool:
        return self._is_enabled

    @property
    def CurrentBoundingRectangle(self) -> FakeRect:
        return self._rect

    def Invoke(self) -> None:
        self.invoke_calls += 1


class RecordingClick:
    """Records every gesture-click seam call; reports a landed click.

    With ``reason`` set it returns the widened 3-tuple seam shape
    ``(succeeded, events_sent, reason)``; otherwise it keeps the legacy
    2-tuple, which the executor must still accept.
    """

    def __init__(
        self,
        *,
        succeeded: bool = True,
        events: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.calls: list[tuple[int, int, str, int]] = []
        self._succeeded = succeeded
        self._events = events
        self._reason = reason

    def __call__(
        self, x: int, y: int, button: str, click_count: int
    ) -> tuple[bool, int] | tuple[bool, int, Optional[str]]:
        self.calls.append((x, y, button, click_count))
        events = (
            self._events if self._events is not None else 2 * click_count
        )
        if self._reason is not None:
            return (self._succeeded, events, self._reason)
        return (self._succeeded, events)


def make_match(
    control: Any,
    *,
    name: str = "Cancel",
    role: str = "button",
    bounds: tuple[int, int, int, int] = (100, 100, 40, 30),
) -> ElementMatch:
    return ElementMatch(
        item_id="item-1",
        display_number=1,
        name=name,
        role=role,
        bounds=bounds,
        monitor_id=0,
        score=0.9,
        is_eligible=True,
        source="uia",
        invoke_supported=True,
        is_enabled=True,
        control_ref=control,
    )


def snap() -> SnapshotForeground:
    return SnapshotForeground(
        window=1000,
        pid=4321,
        process_name="notepad.exe",
        window_creation_time=99,
    )


def matching_probe() -> ForegroundProbe:
    return ForegroundProbe(
        window=1000, pid=4321, process_name="notepad.exe",
        window_creation_time=99,
    )


def make_executor(
    *,
    gesture_click=None,
    coordinate_click=None,
    window_at_point=None,
    point_hits_winner=None,
    on_screen=None,
    invoke_fn=None,
) -> ClickExecutor:
    probe = matching_probe()
    if coordinate_click is None:
        coordinate_click = lambda _x, _y: (True, 2)
    if window_at_point is None:
        window_at_point = lambda _x, _y: probe.window
    if point_hits_winner is None:
        point_hits_winner = lambda _w, _x, _y: True
    if on_screen is None:
        on_screen = lambda _x, _y: True
    if invoke_fn is None:
        invoke_fn = lambda ref: ref.Invoke()
    kwargs: dict[str, Any] = {}
    if gesture_click is not None:
        kwargs["gesture_click_fn"] = gesture_click
    return ClickExecutor(
        coordinate_click_fn=coordinate_click,
        foreground_probe=lambda: probe,
        on_screen_fn=on_screen,
        invoke_fn=invoke_fn,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=point_hits_winner,
        **kwargs,
    )


def query(gesture: ClickGesture = ClickGesture.INVOKE) -> ElementQuery:
    return ElementQuery(
        name="cancel",
        role="button",
        ordinal=None,
        spatial=None,
        raw_utterance="click the cancel button",
        gesture=gesture,
    )


# ---------------------------------------------------------------------------
# ElementQuery.gesture -- default value is today's behaviour.
# ---------------------------------------------------------------------------

class TestElementQueryGestureField:
    """The field exists, defaults to Invoke, and leaves every call site valid."""

    def test_default_gesture_is_invoke(self):
        q = ElementQuery(
            name="cancel", role=None, ordinal=None, spatial=None,
            raw_utterance="click cancel",
        )
        assert q.gesture is ClickGesture.INVOKE

    def test_gesture_can_be_set_explicitly(self):
        q = query(ClickGesture.RIGHT_CLICK)
        assert q.gesture is ClickGesture.RIGHT_CLICK

    def test_gesture_values_are_stable_strings(self):
        # The value crosses the Logic -> Input process boundary; keep the
        # wire-visible strings pinned.
        assert ClickGesture.INVOKE.value == "invoke"
        assert ClickGesture.RIGHT_CLICK.value == "right_click"
        assert ClickGesture.DOUBLE_CLICK.value == "double_click"


# ---------------------------------------------------------------------------
# Executor: a non-default gesture never invokes.
# ---------------------------------------------------------------------------

class TestNonDefaultGestureSkipsInvoke:

    def test_right_click_uses_the_coordinate_path_not_invoke(self):
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "ok"
        assert result.clicked_via == "coordinate"
        assert control.invoke_calls == 0
        assert clicks.calls == [(120, 115, "right", 1)]

    def test_double_click_sends_two_left_clicks(self):
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.DOUBLE_CLICK)
        )
        assert result.outcome == "ok"
        assert result.clicked_via == "coordinate"
        assert control.invoke_calls == 0
        assert clicks.calls == [(120, 115, "left", 2)]

    def test_default_gesture_still_invokes(self):
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(gesture_click=clicks)
        result = ex.click(make_match(control), snap(), query())
        assert result.outcome == "ok"
        assert result.clicked_via == "invoke"
        assert control.invoke_calls == 1
        assert clicks.calls == []


# ---------------------------------------------------------------------------
# Executor: every guard on the coordinate path still applies.
# ---------------------------------------------------------------------------

class TestGestureKeepsEveryGuard:

    def test_obstructed_click_point_refuses_with_no_input(self):
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(
            gesture_click=clicks,
            window_at_point=lambda _x, _y: 777,  # another root window
        )
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "click_point_obstructed"
        assert clicks.calls == []
        assert control.invoke_calls == 0

    def test_same_root_occluder_refuses_with_no_input(self):
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(
            gesture_click=clicks,
            point_hits_winner=lambda _w, _x, _y: False,
        )
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.DOUBLE_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "click_point_obstructed"
        assert clicks.calls == []

    def test_disabled_control_refuses_before_any_click(self):
        control = FakeControl(is_enabled=False)
        clicks = RecordingClick()
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "disabled"
        assert clicks.calls == []

    def test_offscreen_target_refuses_before_any_click(self):
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(
            gesture_click=clicks, on_screen=lambda _x, _y: False
        )
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "target_moved_offscreen"
        assert clicks.calls == []

    def test_ineligible_match_refuses_and_never_invokes(self):
        # A bare substring match fails the stronger coordinate-click
        # eligibility gate; a gesture must not fall back to Invoke instead
        # (Invoke cannot honour the requested button or count).
        control = FakeControl()
        clicks = RecordingClick()
        ex = make_executor(gesture_click=clicks)
        match = make_match(control, name="Cancel all downloads", role="text")
        q = ElementQuery(
            name="downloads", role=None, ordinal=None, spatial=None,
            raw_utterance="right click downloads",
            gesture=ClickGesture.RIGHT_CLICK,
        )
        result = ex.click(match, snap(), q)
        assert result.outcome == "execution_failed"
        assert result.reason == "gesture_not_eligible"
        assert clicks.calls == []
        assert control.invoke_calls == 0

    def test_short_send_reports_sendinput_short(self):
        # A double click expects four events (two down/up pairs); fewer means
        # the OS dropped part of the gesture.
        control = FakeControl()
        clicks = RecordingClick(succeeded=False, events=3)
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.DOUBLE_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "sendinput_short"

    def test_release_failed_from_the_seam_is_button_release_failed(self):
        # wh-mouse-grid.1.5: a partial batch left a button DOWN and the
        # seam's compensating release was refused too. That is a distinct
        # user-facing state (the button may still be held) and must not
        # collapse onto sendinput_short.
        # wh-mouse-grid.1.19: a LEFT-button gesture (double click) keeps the
        # unqualified tag; the recovery wording prescribes a plain click.
        control = FakeControl()
        clicks = RecordingClick(
            succeeded=False, events=1, reason="release_failed"
        )
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.DOUBLE_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "button_release_failed"

    def test_right_click_release_failed_keeps_the_button_identity(self):
        # wh-mouse-grid.1.19: a failed RIGHTUP leaves the RIGHT button held.
        # The generic tag's recovery toast prescribes a plain (left) click,
        # which sends LEFTDOWN+LEFTUP and would NOT release the right button
        # -- so the executor must preserve which button is stuck.
        control = FakeControl()
        clicks = RecordingClick(
            succeeded=False, events=1, reason="release_failed"
        )
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "right_button_release_failed"

    def test_not_landed_send_reports_gesture_failure(self):
        control = FakeControl()
        clicks = RecordingClick(succeeded=False)
        ex = make_executor(gesture_click=clicks)
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "gesture_sendinput_failed"

    def test_unwired_gesture_seam_fails_closed(self):
        # No gesture_click_fn injected: the placeholder raises and the
        # executor maps the raise to a fail-closed refusal -- never a silent
        # success, and never a fall-through to Invoke.
        control = FakeControl()
        ex = make_executor()
        result = ex.click(
            make_match(control), snap(), query(ClickGesture.RIGHT_CLICK)
        )
        assert result.outcome == "execution_failed"
        assert result.reason == "gesture_sendinput_failed"
        assert control.invoke_calls == 0
