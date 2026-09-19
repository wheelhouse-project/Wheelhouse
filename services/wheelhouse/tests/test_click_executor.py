"""Tests for ClickExecutor (wh-mzpvx).

Two suites per the wh-mzpvx bead and the v5 design doc "Test surface":

* EIGHT-CASE integration suite against fake COM elements, exercising every
  pre-click verification failure and every Invoke branch, plus the
  graceful-degrade branch and the stronger coordinate-click eligibility gate.
* SIX-CASE COM-lifetime suite (v4 finding 9) proving the in-flight click holds
  its own COM references and aborts cleanly (never crashes) on a dangling /
  expired proxy.

All seams (Invoke / IsEnabled / BoundingRectangle on control_ref, the
foreground probe, the on-screen check, the coordinate click) are fakes; the
suite runs headless with no real COM, foreground, SendInput, or monitor.

Reason-tag literals asserted here are the wh-mzpvx contract strings the
notice-wording slice keys off; do not loosen them.
"""

from __future__ import annotations

import gc
import logging
import weakref
from typing import Any, Optional

import pytest

from ui.click_executor import (
    ClickExecutor,
    ClickResult,
    ForegroundProbe,
    SnapshotForeground,
)
from ui.element_types import ElementMatch, ElementQuery
from ui.invoke_error_codes import (
    UIA_E_ELEMENTNOTAVAILABLE,
    UIA_E_NOTSUPPORTED,
)

# A COM error code NOT on the no-side-effect allowlist (E_FAIL).
NON_ALLOWLISTED_HRESULT = 0x80004005


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------

class FakeRect:
    """tagRECT-like object: left/top/right/bottom (UIA's native shape)."""

    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class FakeComError(Exception):
    """Stand-in for comtypes.COMError carrying a signed-or-unsigned hresult."""

    def __init__(self, hresult: int) -> None:
        super().__init__(hresult)
        self.hresult = hresult


class FakeControl:
    """A fake control_ref exposing the executor's Current* + Invoke surface.

    ``is_enabled`` / the rect drive the pre-click re-reads; ``invoke_raises``
    (an exception instance) makes Invoke() raise; ``raise_on_enabled`` /
    ``raise_on_bounds`` make the property re-reads raise (a COM property read
    failure). ``invoke_calls`` records how many times Invoke fired.
    """

    def __init__(
        self,
        *,
        is_enabled: bool = True,
        rect: Optional[FakeRect] = None,
        invoke_raises: Optional[BaseException] = None,
        raise_on_enabled: bool = False,
        raise_on_bounds: bool = False,
    ) -> None:
        self._is_enabled = is_enabled
        self._rect = rect if rect is not None else FakeRect(100, 100, 140, 130)
        self._invoke_raises = invoke_raises
        self._raise_on_enabled = raise_on_enabled
        self._raise_on_bounds = raise_on_bounds
        self.invoke_calls = 0

    @property
    def CurrentIsEnabled(self) -> bool:
        if self._raise_on_enabled:
            raise FakeComError(NON_ALLOWLISTED_HRESULT)
        return self._is_enabled

    @property
    def CurrentBoundingRectangle(self) -> FakeRect:
        if self._raise_on_bounds:
            raise FakeComError(NON_ALLOWLISTED_HRESULT)
        return self._rect

    def Invoke(self) -> None:
        self.invoke_calls += 1
        if self._invoke_raises is not None:
            raise self._invoke_raises


def make_match(
    control: Any,
    *,
    name: str = "Cancel",
    role: str = "button",
    is_enabled: bool = True,
    bounds: tuple[int, int, int, int] = (100, 100, 40, 30),
    source_window_is_shell: bool = False,
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
        is_enabled=is_enabled,
        control_ref=control,
        source_window_is_shell=source_window_is_shell,
    )


def snap(
    *,
    window: int = 1000,
    pid: int = 4321,
    process_name: str = "notepad.exe",
    creation_time: int = 99,
) -> SnapshotForeground:
    return SnapshotForeground(
        window=window,
        pid=pid,
        process_name=process_name,
        window_creation_time=creation_time,
    )


def matching_probe(**overrides: Any) -> ForegroundProbe:
    """A probe whose identity matches snap() unless overridden."""
    base: dict[str, Any] = {
        "window": 1000,
        "pid": 4321,
        "process_name": "notepad.exe",
        "window_creation_time": 99,
    }
    base.update(overrides)
    return ForegroundProbe(**base)


def probe_fn(probe: ForegroundProbe):
    return lambda: probe


def always_on_screen(_x: int, _y: int) -> bool:
    return True


def never_on_screen(_x: int, _y: int) -> bool:
    return False


def is_fake_com_error(exc: BaseException) -> bool:
    """Test COM-error predicate: recognise the fake COM-error class.

    The production default (ClickExecutor's _default_is_com_error) checks
    isinstance(exc, comtypes.COMError), which a FakeComError is NOT. The
    executor gates HRESULT allowlisting on this predicate (reviewer_1 finding
    wh-9f3t.28.1), so every test that drives the Invoke COM-error branches must
    inject a predicate that recognises the fake -- otherwise the fakes are
    treated as non-COM exceptions and never reach the allowlist/fallback.
    """
    return isinstance(exc, FakeComError)


def make_executor(
    *,
    probe: Optional[ForegroundProbe] = None,
    on_screen=always_on_screen,
    coordinate_click=None,
    com_error_predicate=is_fake_com_error,
    enable_coordinate_click_on_com_error: bool = False,
    invoke_fn=None,
    overlay_bounds_tolerance_physical_px: Optional[int] = None,
    window_at_point=None,
    point_hits_winner=None,
) -> ClickExecutor:
    if probe is None:
        probe = matching_probe()
    if coordinate_click is None:
        # Default fake coordinate click: succeeds, sends 2 events.
        coordinate_click = lambda _x, _y: (True, 2)
    if window_at_point is None:
        # Default fake click-point hit-test: reports the probe's own window as
        # the root at the point, so the coordinate fallback's obstruction
        # check (wh-explorer-navpane-click.1.1) passes for the standard
        # matching_probe(). Tests of the hit-test itself inject their own.
        expected_window = probe.window
        window_at_point = lambda _x, _y: expected_window
    if point_hits_winner is None:
        # Default fake UIA point check: the element at the point is the
        # winner, so the second obstruction layer
        # (wh-explorer-navpane-click.1.4) passes. Tests of the check itself
        # inject their own.
        point_hits_winner = lambda _w, _x, _y: True
    if invoke_fn is None:
        # The production default fetches the control's UIA Invoke pattern;
        # the FakeControl in this module models the press as a direct
        # ``Invoke()`` method (records invoke_calls / raises invoke_raises),
        # so drive that here. Tests of the real pattern-fetch default live in
        # test_uia_walker.py (invoke_via_invoke_pattern).
        invoke_fn = lambda ref: ref.Invoke()
    kwargs: dict[str, Any] = {}
    if overlay_bounds_tolerance_physical_px is not None:
        kwargs["overlay_bounds_tolerance_physical_px"] = (
            overlay_bounds_tolerance_physical_px
        )
    return ClickExecutor(
        coordinate_click_fn=coordinate_click,
        foreground_probe=probe_fn(probe),
        on_screen_fn=on_screen,
        com_error_predicate=com_error_predicate,
        invoke_fn=invoke_fn,
        enable_coordinate_click_on_com_error=enable_coordinate_click_on_com_error,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=point_hits_winner,
        **kwargs,
    )


QUERY = ElementQuery(
    name="cancel", role="button", ordinal=None, spatial=None,
    raw_utterance="click the cancel button",
)


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------

def test_invoke_success_returns_ok():
    control = FakeControl()
    ex = make_executor()
    result = ex.click(make_match(control), snap(), QUERY)
    assert isinstance(result, ClickResult)
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.matched_name == "Cancel"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1


# ---------------------------------------------------------------------------
# EIGHT-CASE integration suite.
# ---------------------------------------------------------------------------

def test_case1_isenabled_false_is_disabled_not_bounds_invalid():
    # A disabled control is its OWN reason, distinct from bounds_invalid.
    control = FakeControl(is_enabled=False)
    ex = make_executor()
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "disabled"
    assert control.invoke_calls == 0


def test_case2_bounding_rectangle_raises_is_bounds_invalid():
    control = FakeControl(raise_on_bounds=True)
    ex = make_executor()
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_invalid"
    assert control.invoke_calls == 0


def test_case2b_isenabled_read_raises_is_bounds_invalid():
    # A COM error on the IsEnabled re-read is bounds_invalid (v5 step 4).
    control = FakeControl(raise_on_enabled=True)
    ex = make_executor()
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "bounds_invalid"


def test_case2c_empty_rect_is_bounds_invalid():
    control = FakeControl(rect=FakeRect(100, 100, 100, 100))  # zero width+height
    ex = make_executor()
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "bounds_invalid"


def test_case3_offscreen_bounds_is_target_moved_offscreen():
    control = FakeControl()
    ex = make_executor(on_screen=never_on_screen)
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "target_moved_offscreen"
    assert control.invoke_calls == 0


def test_case4_foreground_hwnd_mismatch_is_foreground_changed():
    control = FakeControl()
    ex = make_executor(probe=matching_probe(window=2000))
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "foreground_changed"
    assert control.invoke_calls == 0


def test_case5_hwnd_reused_different_pid_is_foreground_changed():
    # Same HWND, different PID -> HWND reuse caught at step 2.
    control = FakeControl()
    ex = make_executor(probe=matching_probe(pid=9999))
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "foreground_changed"


def test_case5b_process_name_mismatch_is_foreground_changed():
    control = FakeControl()
    ex = make_executor(probe=matching_probe(process_name="evil.exe"))
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "foreground_changed"


def test_case6_pid_reused_different_creation_time_is_foreground_changed():
    # Same HWND + PID + name, different creation time -> PID reuse after exit.
    control = FakeControl()
    ex = make_executor(probe=matching_probe(window_creation_time=12345))
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "foreground_changed"


def test_case7_allowlisted_hresult_falls_through_to_coordinate_click():
    # Invoke raises a no-side-effect HRESULT; match passes stronger eligibility
    # (exact name) -> coordinate-click fallback fires and succeeds.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = make_executor(coordinate_click=coord)
    result = ex.click(
        make_match(control, name="cancel"),  # exact (case-insensitive) match
        snap(),
        QUERY,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    # Centre of the fresh rect (100,100)-(140,130) is (120, 115).
    assert coord_calls == [(120, 115)]


def test_case7b_allowlisted_hresult_invoke_com_error_when_not_eligible():
    # Allowlisted HRESULT but the match is a bare substring+role-mismatch match
    # (fails the stronger eligibility check) -> invoke_com_error, NO coordinate
    # click. name "Welcome cancel screen" contains "cancel" only as a
    # substring (not exact, not a prefix) and role "text" != query role
    # "button", so neither eligibility arm passes.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_ELEMENTNOTAVAILABLE))
    coord_calls: list[Any] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = make_executor(coordinate_click=coord)
    result = ex.click(
        make_match(control, name="Welcome cancel screen", role="text"),
        snap(),
        QUERY,
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_com_error"
    assert coord_calls == []  # the stronger gate blocked the coordinate click


def test_case8_non_allowlisted_hresult_is_invoke_com_error_failclosed():
    # Non-allowlisted HRESULT, knob False -> invoke_com_error, NO coordinate
    # click (the fail-closed default).
    control = FakeControl(invoke_raises=FakeComError(NON_ALLOWLISTED_HRESULT))
    coord_calls: list[Any] = []
    ex = make_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)), (True, 2))[1],
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_com_error"
    assert coord_calls == []  # fail closed: no coordinate click


def test_case8b_non_allowlisted_hresult_knob_true_coordinate_clicks():
    # Same non-allowlisted error but the operator knob is True AND the match is
    # eligible (exact name) -> coordinate-click fallback runs.
    control = FakeControl(invoke_raises=FakeComError(NON_ALLOWLISTED_HRESULT))
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = make_executor(
        coordinate_click=coord,
        enable_coordinate_click_on_com_error=True,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert coord_calls == [(120, 115)]


# ---------------------------------------------------------------------------
# Pre-click bounds-tolerance check (Phase 1.5, design r1c.6).
#
# A control whose freshly-read BoundingRectangle has moved MORE than the
# configured tolerance from its cached walk-time bounds is refused with
# reason "bounds_stale" -- the badge no longer points where the user saw it.
# This is a PARTIAL defence against ONE case in the stale-badge family. The
# honesty tests below DELIBERATELY prove its limits: a within-tolerance move
# and an unchanged-bounds obscuration both PASS the check and the click fires.
# The comparison reuses the BoundingRectangle already read in _verify step 5
# (zero extra Win32 round-trips); the cached ElementMatch.bounds and the parsed
# fresh rect are both (x, y, w, h), so they are compared component-by-component.
# ---------------------------------------------------------------------------

def test_bounds_moved_beyond_tolerance_is_bounds_stale():
    # Cached bounds (100,100,40,30); the control's fresh rect has moved its
    # top-left x to 120 (a 20px shift) -- MORE than the default 8px tolerance.
    # The executor refuses with "bounds_stale" and NEVER invokes.
    # FakeRect(120, 100, 160, 130) parses to (x=120, y=100, w=40, h=30):
    # abs(120 - 100) == 20 > 8.
    control = FakeControl(rect=FakeRect(120, 100, 160, 130))
    ex = make_executor()
    result = ex.click(
        make_match(control, bounds=(100, 100, 40, 30)), snap(), QUERY
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_stale"
    assert control.invoke_calls == 0  # never clicked a control the user can't see


def test_bounds_within_tolerance_passes_and_clicks_HONESTY():
    # HONESTY TEST (design r1c.6): a within-tolerance move is NOT caught. A 4px
    # shift with the default 8px tolerance passes the check and the click fires.
    # This documents the accepted limit -- the badge may be slightly off but the
    # design does not refuse the click for a small scroll.
    # FakeRect(104, 100, 144, 130) parses to (x=104, y=100, w=40, h=30):
    # abs(104 - 100) == 4 <= 8 in every dimension.
    control = FakeControl(rect=FakeRect(104, 100, 144, 130))
    ex = make_executor()
    result = ex.click(
        make_match(control, bounds=(100, 100, 40, 30)), snap(), QUERY
    )
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1  # the within-tolerance move reached invoke


def test_bounds_unchanged_obscuration_passes_and_clicks_HONESTY():
    # HONESTY TEST (design r1c.6): the bounds-tolerance check has NO signal for
    # an obscuring overlay (a modal dialog or popup that appears OVER the
    # focused window) that does not move the underlying control. The fresh
    # bounds equal the cached bounds, so the check passes and the click fires on
    # the now-obscured control. The only defence here is the user re-saying
    # "show numbers".
    # FakeRect(100, 100, 140, 130) parses to (x=100, y=100, w=40, h=30) == cached.
    control = FakeControl(rect=FakeRect(100, 100, 140, 130))
    ex = make_executor()
    result = ex.click(
        make_match(control, bounds=(100, 100, 40, 30)), snap(), QUERY
    )
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1  # unchanged bounds -> obscuration not caught


def test_bounds_height_change_beyond_tolerance_is_bounds_stale():
    # Per-dimension rule: a change in ANY of x/y/w/h beyond the tolerance fires
    # bounds_stale. Here only the height changes: cached h=30, fresh h=60
    # (abs(60 - 30) == 30 > 8). x/y/w are unchanged.
    # FakeRect(100, 100, 140, 160) parses to (x=100, y=100, w=40, h=60).
    control = FakeControl(rect=FakeRect(100, 100, 140, 160))
    ex = make_executor()
    result = ex.click(
        make_match(control, bounds=(100, 100, 40, 30)), snap(), QUERY
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_stale"
    assert control.invoke_calls == 0


def test_bounds_exactly_at_tolerance_passes():
    # Boundary: a shift of EXACTLY the tolerance (8px) is accepted (the rule is
    # "<= tolerance passes"). Cached x=100, fresh x=108 -> abs == 8 == tolerance.
    # FakeRect(108, 100, 148, 130) parses to (x=108, y=100, w=40, h=30).
    control = FakeControl(rect=FakeRect(108, 100, 148, 130))
    ex = make_executor(overlay_bounds_tolerance_physical_px=8)
    result = ex.click(
        make_match(control, bounds=(100, 100, 40, 30)), snap(), QUERY
    )
    assert result.outcome == "ok"
    assert control.invoke_calls == 1


def test_bounds_zero_tolerance_requires_exact_match():
    # A tolerance of 0 means exact match: any nonzero shift in any dimension
    # fires bounds_stale. Cached (100,100,40,30); fresh shifted x by 1.
    # FakeRect(101, 100, 141, 130) parses to (x=101, y=100, w=40, h=30).
    control = FakeControl(rect=FakeRect(101, 100, 141, 130))
    ex = make_executor(overlay_bounds_tolerance_physical_px=0)
    result = ex.click(
        make_match(control, bounds=(100, 100, 40, 30)), snap(), QUERY
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_stale"
    assert control.invoke_calls == 0


def test_bounds_cached_none_is_bounds_invalid():
    # Robustness (reviewer_1 wh-n29v.89.1): the cached-bounds comparison is the
    # only read in _verify that previously unpacked winner.bounds with no guard.
    # ElementMatch.bounds is typed (x, y, w, h) and the walker's _rect_to_bounds
    # always builds that shape, so a None/malformed cached bounds CANNOT occur in
    # production. The guard exists so a contract-violating winner fails CLOSED to
    # bounds_invalid like every other read in _verify, instead of raising an
    # unpack error that escapes to the handler's generic execution-failed path
    # with no matched-name context. The fresh rect here would otherwise pass.
    control = FakeControl(rect=FakeRect(100, 100, 140, 130))
    ex = make_executor()
    none_bounds: Any = None
    result = ex.click(make_match(control, bounds=none_bounds), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_invalid"
    assert control.invoke_calls == 0  # never invoked on malformed cached bounds


def test_bounds_cached_malformed_length_is_bounds_invalid():
    # A cached bounds tuple of the wrong length (here 2 elements) also fails
    # closed to bounds_invalid rather than raising a "not enough values to
    # unpack" error inside _verify. Same fail-closed contract as the None case.
    control = FakeControl(rect=FakeRect(100, 100, 140, 130))
    ex = make_executor()
    short_bounds: Any = (100, 100)
    result = ex.click(make_match(control, bounds=short_bounds), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_invalid"
    assert control.invoke_calls == 0


# ---------------------------------------------------------------------------
# Graceful-degrade branch (creation-time read failed).
# ---------------------------------------------------------------------------

def test_graceful_degrade_accepts_when_hwnd_pid_name_match():
    # Creation-time read denied (None) but HWND + PID + name all match ->
    # accept and click (the admin-elevated foreground case).
    control = FakeControl()
    ex = make_executor(probe=matching_probe(window_creation_time=None))
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "ok"
    assert control.invoke_calls == 1


def test_graceful_degrade_failed_when_lesser_check_incomplete():
    # Creation-time read denied AND PID could not be read (None) -> a lesser
    # check could not complete -> foreground_verification_failed (distinct from
    # foreground_changed).
    control = FakeControl()
    ex = make_executor(
        probe=matching_probe(pid=None, window_creation_time=None)
    )
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "foreground_verification_failed"
    assert control.invoke_calls == 0


def test_graceful_degrade_failed_when_process_name_incomplete():
    # Creation-time denied AND process-name could not be read -> verification
    # failed (the lesser process-name check could not complete).
    control = FakeControl()
    ex = make_executor(
        probe=matching_probe(process_name=None, window_creation_time=None)
    )
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "foreground_verification_failed"


def test_pid_disagreement_wins_over_verification_failed():
    # Even if creation-time is denied, a DISAGREEING PID is a real change ->
    # foreground_changed, never foreground_verification_failed.
    control = FakeControl()
    ex = make_executor(
        probe=matching_probe(pid=9999, window_creation_time=None)
    )
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.reason == "foreground_changed"


# ---------------------------------------------------------------------------
# Stronger coordinate-click eligibility gate.
# ---------------------------------------------------------------------------

def test_stronger_eligibility_substring_role_match_blocks_coordinate_click():
    # Bare substring + role match (NOT exact, NOT starts-with) on an
    # allowlisted HRESULT must produce invoke_com_error, never a coordinate
    # click of a possibly-wrong region. Make role NOT match so the
    # role-AND-enabled arm cannot rescue it -- "cancel" is a substring of the
    # name but neither exact nor a prefix.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    coord_calls: list[Any] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = make_executor(coordinate_click=coord)
    result = ex.click(
        make_match(control, name="Please cancel now", role="text"),
        snap(),
        QUERY,  # query role is "button", control role is "text" -> no role match
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_com_error"
    assert coord_calls == []  # the stronger gate blocked the coordinate click


def test_stronger_eligibility_starts_with_match_allows_coordinate_click():
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    coord_calls: list[tuple[int, int]] = []
    ex = make_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    # name "Cancel all" starts with query name "cancel" (case-insensitive).
    result = ex.click(
        make_match(control, name="Cancel all", role="text"),
        snap(),
        QUERY,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert coord_calls == [(120, 115)]


def test_stronger_eligibility_role_and_enabled_match_allows_coordinate_click():
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    coord_calls: list[tuple[int, int]] = []
    ex = make_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    # name is a bare substring but role matches "button" AND is_enabled True.
    result = ex.click(
        make_match(control, name="Confirm cancel", role="button", is_enabled=True),
        snap(),
        QUERY,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"


# ---------------------------------------------------------------------------
# Coordinate-click failure modes.
# ---------------------------------------------------------------------------

def test_coordinate_click_short_send_is_sendinput_short():
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    # events_sent 1 < expected 2 -> sendinput_short.
    ex = make_executor(coordinate_click=lambda _x, _y: (True, 1))
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "sendinput_short"


def test_coordinate_click_zero_event_send_is_sendinput_nothing_sent():
    # events_sent 0 = the seam refused before any button event went out --
    # provably nothing fired, distinct from a half-fired partial send whose
    # button may be stuck DOWN. With no no_input_fallback installed the
    # refusal keeps its own tag instead of collapsing onto sendinput_short
    # (wh-review-click-numbers.2 part A).
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    ex = make_executor(coordinate_click=lambda _x, _y: (False, 0))
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "sendinput_nothing_sent"


def test_coordinate_click_release_failed_is_button_release_failed():
    # wh-mouse-grid.1.5: the production seam reports (False, 1,
    # "release_failed") when a partial batch left the button DOWN and the
    # compensating release was refused too. The executor must surface that
    # distinct state instead of collapsing it onto sendinput_short -- the
    # user has to be told the button may still be held.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    ex = make_executor(
        coordinate_click=lambda _x, _y: (False, 1, "release_failed")
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "button_release_failed"


def test_coordinate_click_three_tuple_without_reason_stays_short_send():
    # The widened seam contract is (succeeded, events_sent[, reason]); a
    # 3-tuple whose reason is None classifies exactly like the legacy shape.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    ex = make_executor(coordinate_click=lambda _x, _y: (True, 1, None))
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "sendinput_short"


def test_coordinate_click_failure_is_invoke_then_sendinput_failed():
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    ex = make_executor(coordinate_click=lambda _x, _y: (False, 2))
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_then_sendinput_failed"


def test_reverification_failure_during_fallback_returns_its_reason():
    # The world moves between the Invoke attempt and the fallback: the
    # re-verification finds the foreground changed -> return foreground_changed,
    # NOT invoke_then_sendinput_failed. Use a probe that matches on the FIRST
    # verification (pre-Invoke) but mismatches on the re-verification.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    probes = [matching_probe(), matching_probe(window=2000)]

    def changing_probe() -> ForegroundProbe:
        return probes.pop(0) if len(probes) > 1 else probes[0]

    coord_calls: list[Any] = []
    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=changing_probe,
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "foreground_changed"
    assert coord_calls == []  # re-verification aborted before the click


# ---------------------------------------------------------------------------
# reviewer_1 (codex) regressions.
# ---------------------------------------------------------------------------

def test_non_com_exception_with_allowlisted_hresult_is_invoke_com_error():
    # reviewer_1 finding wh-9f3t.28.1: a NON-COM exception that merely carries an
    # allowlisted .hresult attribute must NOT be classified side-effect-free. The
    # com_error_predicate (here is_fake_com_error) rejects it, so it fails closed
    # to invoke_com_error with NO coordinate click -- even with the operator knob
    # True AND an eligible exact-name match (the most permissive config).
    class NotAComError(Exception):
        def __init__(self, hresult: int) -> None:
            super().__init__("not a real COM error")
            self.hresult = hresult

    control = FakeControl(invoke_raises=NotAComError(UIA_E_NOTSUPPORTED))
    coord_calls: list[Any] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = make_executor(
        coordinate_click=coord, enable_coordinate_click_on_com_error=True
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_com_error"
    assert coord_calls == []  # a non-COM exception never reaches the fallback


def test_coordinate_click_seam_exception_maps_to_fail_reason():
    # reviewer_1 finding wh-9f3t.28.2: a coordinate-click seam that RAISES (a real
    # SendInput/Win32 failure on some platforms) must NOT propagate out of
    # click(); it maps to the fallback fail_reason so the one-response contract
    # holds and the user-visible failure notice is not dropped.
    control = FakeControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))

    def raising_coord(_x: int, _y: int) -> tuple[bool, int]:
        raise OSError("SendInput failed at the platform boundary")

    ex = make_executor(coordinate_click=raising_coord)
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_then_sendinput_failed"


# ---------------------------------------------------------------------------
# reviewer_2 (deepseek) regression.
# ---------------------------------------------------------------------------

def test_parse_rect_accepts_4_tuple_bounding_rectangle():
    # reviewer_2 finding wh-9f3t.29.1: _parse_rect has a 4-sequence fallback
    # (left, top, right, bottom) for the unlikely case UIA returns a plain tuple
    # instead of a tagRECT-like object. Every other test uses FakeRect (the
    # object path), leaving the tuple path untested. Drive it through the
    # coordinate fallback (allowlisted HRESULT + exact-name match) so the asserted
    # click centre proves the tuple was parsed correctly: (100,100,140,130) ->
    # left/top (100,100), width/height (40,30), centre (120,115).
    class TupleRectControl(FakeControl):
        @property
        def CurrentBoundingRectangle(self) -> Any:
            return (100, 100, 140, 130)

    control = TupleRectControl(invoke_raises=FakeComError(UIA_E_NOTSUPPORTED))
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = make_executor(coordinate_click=coord)
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert coord_calls == [(120, 115)]


# ---------------------------------------------------------------------------
# SIX-CASE COM-lifetime suite (v4 finding 9).
#
# Rewritten per reviewer_0 finding wh-9f3t.27.1: the earlier versions pinned
# the control in a live local, so gc.collect() collected nothing the click
# needed -- they proved "CPython keeps a referenced object", not the real
# invariant. The executor reads control_ref ONLY through its `winner` argument,
# which pins the proxy on the executor's call frame for the whole click(); the
# caller's keepalive obligation is documented on click(). These tests now prove
# that frame-pin contract directly: each drops the caller's only strong
# reference to the control DURING the click (inside an injected seam that runs
# mid-call) and uses a weakref to prove the proxy is still alive solely because
# the executor's frame holds it, OR drives a genuinely dead proxy (a COM re-read
# that raises) and asserts a clean abort rather than a crash. A weakref that
# survives the mid-call gc is meaningful: if the executor copied out the bits it
# needed and dropped `winner` early, the weakref would clear and the assert
# would fail.
# ---------------------------------------------------------------------------

def test_lifetime1_executor_frame_pins_control_after_caller_drops_ref():
    # The caller's only strong reference to the control is dropped mid-click
    # (inside the foreground probe, which runs first in _verify, after the
    # executor already holds `winner`). gc then runs. The proxy must still be
    # alive -- pinned solely by the executor's `winner` argument -- and the
    # click must complete. The weakref surviving the gc is the proof.
    # The match is passed via holder.pop(), so NO caller-side reference survives
    # into the call: once click() begins, the only reference to the ElementMatch
    # (and thus to its control_ref proxy) is the executor's `winner` argument.
    # The foreground probe runs first in _verify, gc-collects, and the weakref to
    # the control must still be alive -- proof that the executor's frame is the
    # keepalive. If the executor extracted primitives and dropped `winner` early,
    # the weakref would clear and this assert would fail.
    holder: dict[str, Any] = {"match": make_match(FakeControl())}
    control_wr = weakref.ref(holder["match"].control_ref)
    observed: dict[str, bool] = {}

    def dropping_probe() -> ForegroundProbe:
        gc.collect()
        observed["alive_midcall"] = control_wr() is not None
        return matching_probe()

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=dropping_probe,
        on_screen_fn=always_on_screen,
        invoke_fn=lambda ref: ref.Invoke(),
    )
    result = ex.click(holder.pop("match"), snap(), QUERY)
    assert result.outcome == "ok"
    # The proxy survived the mid-call gc with no caller-side reference: the
    # executor's frame was the keepalive.
    assert observed["alive_midcall"] is True


def test_lifetime2_executor_touches_only_the_passed_winner():
    # Two independent matches (two walks). The executor must operate ONLY on the
    # winner it was handed and never reach into the other walk's control, even
    # after that other match is dropped and collected.
    control_a = FakeControl()
    control_b = FakeControl()
    match_a = make_match(control_a, name="cancel")
    match_b = make_match(control_b, name="ok")
    control_b_wr = weakref.ref(control_b)
    del match_b, control_b  # second walk fully released before the first click
    gc.collect()
    assert control_b_wr() is None  # the unrelated walk's control is gone

    ex = make_executor()
    result = ex.click(match_a, snap(), QUERY)
    assert result.outcome == "ok"
    assert control_a.invoke_calls == 1  # only the passed winner was clicked


def test_lifetime3_second_walk_collected_midclick_first_click_completes():
    # A second walk's match is created AND collected at the moment of
    # verification; the in-flight click holds its own winner and completes
    # against the original control, unaffected by the churn.
    control = FakeControl()
    match = make_match(control)
    churn: dict[str, Any] = {}

    def churning_probe() -> ForegroundProbe:
        other = make_match(FakeControl(), name="other")
        churn["ref"] = weakref.ref(other.control_ref)
        del other
        gc.collect()
        return matching_probe()

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=churning_probe,
        on_screen_fn=always_on_screen,
        invoke_fn=lambda ref: ref.Invoke(),
    )
    result = ex.click(match, snap(), QUERY)
    assert result.outcome == "ok"
    assert control.invoke_calls == 1
    assert churn["ref"]() is None  # the churned walk's control was collected
    assert match.control_ref is control  # the winner the executor held survives


def test_lifetime4_gc_between_isenabled_and_bounds_reads_proxy_survives():
    # Drop the caller's last reference AND gc between the IsEnabled re-read and
    # the BoundingRectangle re-read. The BoundingRectangle read must still
    # succeed because the executor's frame pins the proxy across the collection.
    control_wr_box: dict[str, Any] = {}
    observed: dict[str, bool] = {}

    class GcBetweenReadsControl(FakeControl):
        @property
        def CurrentIsEnabled(self) -> bool:
            # Mid-verification (between step 4 and step 5), no caller-side ref to
            # the match survives: gc, then confirm the proxy is still alive
            # (executor frame holds winner) before the next COM read.
            gc.collect()
            observed["alive_between_reads"] = control_wr_box["wr"]() is not None
            return True

    holder: dict[str, Any] = {"match": make_match(GcBetweenReadsControl())}
    control_wr_box["wr"] = weakref.ref(holder["match"].control_ref)
    ex = make_executor()
    result = ex.click(holder.pop("match"), snap(), QUERY)
    assert result.outcome == "ok"
    assert observed["alive_between_reads"] is True


def test_lifetime5_interleaved_unrelated_call_does_not_release_winner():
    # An unrelated UIA round-trip between walk and Invoke must not release the
    # click's winner. Drop the caller ref, run the unrelated call, gc, and prove
    # the winner's proxy is still alive (executor frame) and the click succeeds.
    holder: dict[str, Any] = {"match": make_match(FakeControl())}
    control_wr = weakref.ref(holder["match"].control_ref)
    observed: dict[str, bool] = {}

    def probe_with_unrelated_call() -> ForegroundProbe:
        unrelated = FakeControl()
        unrelated.Invoke()  # a real, unrelated UIA round-trip
        del unrelated
        gc.collect()
        observed["winner_alive"] = control_wr() is not None
        return matching_probe()

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_with_unrelated_call,
        on_screen_fn=always_on_screen,
        invoke_fn=lambda ref: ref.Invoke(),
    )
    result = ex.click(holder.pop("match"), snap(), QUERY)
    assert result.outcome == "ok"
    assert observed["winner_alive"] is True


def test_lifetime6_dead_proxy_on_reread_aborts_cleanly_no_crash():
    # A genuinely dead/expired proxy: the element was destroyed, so a COM
    # property re-read raises UIA_E_ELEMENTNOTAVAILABLE. Foreground identity
    # still matches (so the abort is NOT a foreground change -- the proxy IS
    # touched), and the executor must abort cleanly with bounds_invalid rather
    # than crashing or proceeding to Invoke. This is the real "aborts cleanly on
    # a dangling proxy" invariant the earlier foreground-change model missed.
    class DeadProxyControl(FakeControl):
        @property
        def CurrentBoundingRectangle(self) -> FakeRect:
            raise FakeComError(UIA_E_ELEMENTNOTAVAILABLE)

    dead = DeadProxyControl()
    match = make_match(dead)
    ex = make_executor()  # foreground matches; the proxy is reached and raises
    result = ex.click(match, snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "bounds_invalid"
    assert dead.invoke_calls == 0  # never reached Invoke; no crash


# ---------------------------------------------------------------------------
# Locale-invariant coordinate-fallback role gate (wh-l4h.1.15).
#
# _coord_eligible's role-match leg must compare the numeric control_type_id
# (canonical query.role name -> id) rather than the localized role STRING, so
# a role-qualified query passes the coordinate gate on non-English Windows.
# control_type_id == 0 (or a query.role outside the canonical name map) falls
# back to the localized-string casefold comparison.
# ---------------------------------------------------------------------------

UIA_BUTTON_ID = 50000
UIA_EDIT_ID = 50004


def _coord_query(name: str, role: Optional[str]) -> ElementQuery:
    return ElementQuery(
        name=name, role=role, ordinal=None, spatial=None,
        raw_utterance=f"click {name}",
    )


def _coord_match(
    *,
    name: str,
    role: str,
    control_type_id: int,
    is_enabled: bool = True,
) -> ElementMatch:
    return ElementMatch(
        item_id="item-1",
        display_number=1,
        name=name,
        role=role,
        bounds=(100, 100, 40, 30),
        monitor_id=0,
        score=0.9,
        is_eligible=True,
        source="uia",
        invoke_supported=True,
        is_enabled=is_enabled,
        control_ref=object(),
        control_type_id=control_type_id,
    )


def test_coord_eligible_localized_role_passes_by_id():
    # German localized role "Schaltflaeche" with the correct Button id passes
    # the role-match-AND-enabled leg even though the role STRING does not match
    # the canonical "Button". Name is a coincidental substring only (not exact /
    # starts-with), so only the role leg can carry eligibility.
    q = _coord_query("el", role="Button")
    m = _coord_match(name="Cancel", role="Schaltflaeche", control_type_id=UIA_BUTTON_ID)
    assert ClickExecutor._coord_eligible(m, q) is True


def test_coord_eligible_wrong_id_fails():
    # Wrong control_type_id (Edit, not Button) -> role leg fails; the name is
    # not exact / starts-with, so the match is not coordinate-eligible.
    q = _coord_query("el", role="Button")
    m = _coord_match(name="Cancel", role="Schaltflaeche", control_type_id=UIA_EDIT_ID)
    assert ClickExecutor._coord_eligible(m, q) is False


def test_coord_eligible_zero_id_falls_back_to_string():
    # control_type_id == 0 -> fall back to localized-string casefold. English
    # "Button" string matches the canonical "Button" role, enabled -> eligible.
    q = _coord_query(" el", role="Button")
    m = _coord_match(name="Cancel", role="Button", control_type_id=0)
    assert ClickExecutor._coord_eligible(m, q) is True


def test_coord_eligible_zero_id_string_mismatch_fails():
    # control_type_id == 0 falls back to string; localized "Schaltflaeche" does
    # not equal canonical "Button" -> role leg fails, name not exact/starts-with.
    q = _coord_query(" el", role="Button")
    m = _coord_match(name="Cancel", role="Schaltflaeche", control_type_id=0)
    assert ClickExecutor._coord_eligible(m, q) is False


def test_coord_eligible_role_not_in_map_falls_back_to_string():
    # query.role outside the canonical name map (defensive) falls back to the
    # string comparison even when control_type_id is set.
    q = _coord_query("el", role="ScrollBar")
    m = _coord_match(name="Cancel", role="ScrollBar", control_type_id=UIA_BUTTON_ID)
    assert ClickExecutor._coord_eligible(m, q) is True


def test_coord_eligible_disabled_role_match_still_fails():
    # The role leg requires is_enabled True; a disabled control whose id matches
    # is still not coordinate-eligible (unchanged gate semantics).
    q = _coord_query("el", role="Button")
    m = _coord_match(
        name="Cancel", role="Schaltflaeche", control_type_id=UIA_BUTTON_ID,
        is_enabled=False,
    )
    assert ClickExecutor._coord_eligible(m, q) is False


def test_coord_eligible_none_role_not_a_match():
    # query.role is None means no role constraint, which is NOT a role match
    # (the wh-9f3t.2.1 row-(e) rule), so the role leg is skipped. The name is a
    # coincidental substring only (not exact / starts-with), so a no-role query
    # is not coordinate-eligible even when the control is an enabled Button with
    # the correct id. Mirrors the scorer side's None-role regression test for
    # the second comparison site (wh-9f3t.67.1).
    q = _coord_query("el", role=None)
    m = _coord_match(name="Cancel", role="Schaltflaeche", control_type_id=UIA_BUTTON_ID)
    assert ClickExecutor._coord_eligible(m, q) is False


# ---------------------------------------------------------------------------
# Default press path (wh-click-invoke-on-element-not-pattern).
#
# The executor's earlier ``winner.control_ref.Invoke()`` raised
# AttributeError on every real control because IUIAutomationElement has no
# Invoke method. The first fix swapped in ``GetCachedPatternAs(id, iid)``,
# which returns a raw int -- so ``.Invoke()`` raised AttributeError again,
# one level down (reviewer_0 finding .1.1). These tests pin the corrected
# behaviour: the constructor default is the pattern-fetch helper, and a
# faithful fake element (Current* property reads + ``GetCachedPattern``
# returning a raw pointer whose ``QueryInterface`` yields the typed Invoke
# pattern, NO direct Invoke and NO ``*As`` variant) clicks ``ok`` through
# that default. Modelling the QueryInterface hop is the regression fence:
# the int-returning ``*As`` shape would fail here.
# ---------------------------------------------------------------------------

class _FakeInvokePattern:
    def __init__(self) -> None:
        self.invoke_calls = 0

    def Invoke(self) -> None:
        self.invoke_calls += 1


class _FakeRawPattern:
    """Models the raw POINTER(IUnknown) returned by GetCachedPattern: truthy,
    QueryInterface yields the typed pattern, NO direct Invoke.
    """

    def __init__(self, typed: _FakeInvokePattern) -> None:
        self._typed = typed

    def QueryInterface(self, _iface: Any) -> _FakeInvokePattern:
        return self._typed


class FaithfulElement:
    """Models a real cached IUIAutomationElement: Current* property reads
    and the Invoke pattern reached via ``GetCachedPattern`` + QueryInterface,
    with NO direct Invoke method (a real element has none).
    """

    def __init__(self, rect: FakeRect) -> None:
        self._rect = rect
        self.pattern = _FakeInvokePattern()

    @property
    def CurrentIsEnabled(self) -> bool:
        return True

    @property
    def CurrentBoundingRectangle(self) -> FakeRect:
        return self._rect

    def GetCachedPattern(self, pattern_id: Any) -> _FakeRawPattern:
        return _FakeRawPattern(self.pattern)


def test_constructor_default_invoke_fn_is_pattern_fetch():
    from ui.uia_walker import invoke_via_invoke_pattern

    ex = ClickExecutor(
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
    )
    assert ex._invoke_fn is invoke_via_invoke_pattern


def test_constructor_default_dda_fn_is_legacy_pattern_press():
    """The DoDefaultAction fallback default must be the REAL production press
    (do_default_action_via_legacy_pattern), not the fail-closed placeholder.

    This is the regression fence for wh-click-dda-wiring: the executor's seam
    and branch handling shipped fully built but the default was left as the
    placeholder, so every InvokePattern-less control (e.g. a Gmail message row)
    failed dda_unavailable. The production-wiring slice connects the real press
    as the default, exactly like invoke_fn defaults to invoke_via_invoke_pattern.
    """
    from ui.uia_walker import do_default_action_via_legacy_pattern

    ex = ClickExecutor(
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
    )
    assert ex._do_default_action_fn is do_default_action_via_legacy_pattern


def test_default_press_path_invokes_through_the_pattern():
    from ui.uia_walker import invoke_via_invoke_pattern

    control = FaithfulElement(FakeRect(100, 100, 140, 130))
    ex = make_executor(invoke_fn=invoke_via_invoke_pattern)

    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)

    assert result.outcome == "ok"
    assert control.pattern.invoke_calls == 1


def test_invoke_pattern_unavailable_enters_dda_fallback_no_coord_on_default():
    """When ``InvokePattern`` is structurally unavailable
    (``InvokePatternUnavailable``, a non-COM RuntimeError), the executor now
    enters the DoDefaultAction (MSAA accDoDefaultAction) press fallback
    (wh-l4h.1.17) instead of terminating at ``invoke_pattern_unavailable``.

    With no ``do_default_action_fn`` injected, the real default
    (``do_default_action_via_legacy_pattern``) runs; ``FakeControl`` exposes no
    Legacy pattern (no ``GetCurrentPattern``), so the press path resolves to no
    pattern and raises ``DoDefaultActionUnavailable``, giving ``dda_unavailable``.
    The match is deliberately INELIGIBLE for the coordinate gate (bare
    substring name, role mismatch) so the structural coordinate fallback
    (wh-explorer-navpane-click) stays out of the way and the knob route is
    what is pinned: not a COM error, so the knob must never coordinate-click.
    """
    from ui.uia_walker import InvokePatternUnavailable

    def raise_unavailable(_ref):
        raise InvokePatternUnavailable("control exposes no UIA Invoke pattern")

    control = FakeControl(rect=FakeRect(100, 100, 140, 130))
    coord_calls = []
    ex = make_executor(
        invoke_fn=raise_unavailable,
        coordinate_click=lambda x, y: (coord_calls.append((x, y)), (True, 2))[1],
        enable_coordinate_click_on_com_error=True,
    )

    result = ex.click(
        make_match(control, name="Please cancel now", role="text"), snap(), QUERY
    )

    assert result.outcome == "execution_failed"
    assert result.reason == "dda_unavailable"
    # Not a COM error and not coordinate-eligible -> no coordinate click,
    # even with the knob on.
    assert coord_calls == []


# ---------------------------------------------------------------------------
# DoDefaultAction (MSAA LegacyIAccessible accDoDefaultAction) press fallback
# (wh-l4h.1.17).
#
# Attempted ONLY in the InvokePatternUnavailable branch -- when InvokePattern is
# structurally unavailable. NEVER attempted when InvokePattern is available but
# returns a COM error. The five paths:
#   dda_ok                      -- DoDefaultAction succeeds -> ok
#   dda_no_default_action       -- pattern present, no default action -> fail
#   dda_unavailable             -- Legacy/DoDefaultAction unavailable -> fail
#   dda_no_side_effect_then_coord -- DoDefaultAction fails with a proven
#                                  side-effect-free HRESULT, gated coord click
#   HONESTY                     -- DoDefaultAction returns a non-success HRESULT
#                                  NOT on the allowlist -> fail closed, no coord
# ---------------------------------------------------------------------------

from ui.click_executor import (  # noqa: E402 -- grouped with the DDA suite
    DoDefaultActionUnavailable,
    NoDefaultAction,
)
from ui.uia_walker import InvokePatternUnavailable as _IPU  # noqa: E402


def _raise_invoke_unavailable(_ref):
    raise _IPU("control exposes no UIA Invoke pattern")


def test_dda_ok_success_returns_ok_via_invoke(caplog):
    # InvokePattern is structurally unavailable; DoDefaultAction succeeds (the
    # seam returns normally). The press happened through the MSAA pattern, so
    # the result is ok with clicked_via "invoke" (a real press, not a coord
    # click). The coordinate seam must NOT fire.
    control = FakeControl()
    coord_calls: list[Any] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    dda_calls: list[Any] = []

    def dda(ref: Any) -> None:
        dda_calls.append(ref)
        return None  # success

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
    )
    # The success-path telemetry marker dda_ok is emitted only as log text
    # (it is not a ClickResult.reason value), so assert it via caplog -- the
    # slice contract requires the dda_ok telemetry tag, and a rename of the
    # marker must not pass silently (reviewer_1 finding wh-n29v.27.2).
    with caplog.at_level(logging.DEBUG, logger="ui.click_executor"):
        result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.clicked_via == "invoke"
    assert len(dda_calls) == 1
    assert dda_calls[0] is control
    assert coord_calls == []
    assert "dda_ok" in caplog.text


def test_dda_no_default_action_fails_with_its_reason():
    # The Legacy pattern is present but the control has no default action AND
    # the match fails the stronger coordinate eligibility gate (bare substring
    # name, role mismatch) -> dda_no_default_action, fail closed, no
    # coordinate click. An ELIGIBLE match now falls through to the structural
    # coordinate fallback instead (wh-explorer-navpane-click) -- see the
    # structural-absence suite at the end of this module.
    control = FakeControl()
    coord_calls: list[Any] = []

    def dda(_ref: Any) -> None:
        raise NoDefaultAction("control exposes no MSAA default action")

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        enable_coordinate_click_on_com_error=True,  # most permissive; still no coord
    )
    result = ex.click(
        make_match(control, name="Please cancel now", role="text"), snap(), QUERY
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "dda_no_default_action"
    assert coord_calls == []


def test_dda_unavailable_fails_with_its_reason():
    # InvokePattern unavailable AND LegacyIAccessible/DoDefaultAction itself
    # unavailable AND the match fails the stronger coordinate eligibility gate
    # -> dda_unavailable, fail closed, no coordinate click. An ELIGIBLE match
    # now falls through to the structural coordinate fallback instead
    # (wh-explorer-navpane-click).
    control = FakeControl()
    coord_calls: list[Any] = []

    def dda(_ref: Any) -> None:
        raise DoDefaultActionUnavailable("no LegacyIAccessible pattern")

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        enable_coordinate_click_on_com_error=True,
    )
    result = ex.click(
        make_match(control, name="Please cancel now", role="text"), snap(), QUERY
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "dda_unavailable"
    assert coord_calls == []


def test_dda_no_side_effect_then_coord_fires_gated_coordinate_click(caplog):
    # DoDefaultAction fails with a proven side-effect-free HRESULT and the match
    # passes the stronger eligibility check (exact name) -> gated coordinate
    # click fires and succeeds. Reason tag dda_no_side_effect_then_coord on the
    # path; outcome ok via coordinate.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    def dda(_ref: Any) -> None:
        raise FakeComError(UIA_E_NOTSUPPORTED)

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )
    # The side-effect-free-then-coordinate telemetry marker
    # dda_no_side_effect_then_coord is emitted only as log text (it is not a
    # ClickResult.reason value), so assert it via caplog -- the slice contract
    # requires this telemetry tag and a rename must not pass silently
    # (reviewer_1 finding wh-n29v.27.2).
    with caplog.at_level(logging.INFO, logger="ui.click_executor"):
        result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    # Centre of (100,100)-(140,130) is (120, 115).
    assert coord_calls == [(120, 115)]
    assert "dda_no_side_effect_then_coord" in caplog.text


def test_dda_no_side_effect_then_coord_delivery_failure_distinct_reason():
    # DoDefaultAction fails with a proven side-effect-free HRESULT and the match
    # passes the stronger eligibility gate, so the gated coordinate retry fires
    # -- but the coordinate click itself fails to land (succeeded=False, with
    # enough events to clear the sendinput_short check). This delivery failure
    # must NOT collapse onto the honesty-boundary dda_no_default_action_failed
    # tag (a may-have-fired failure); it gets its own distinct
    # dda_no_side_effect_then_sendinput_failed tag, mirroring the Invoke path's
    # invoke_then_sendinput_failed (reviewer_2 finding wh-n29v.28.1).
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (False, 2)  # clears sendinput_short, but the click did not land

    def dda(_ref: Any) -> None:
        raise FakeComError(UIA_E_NOTSUPPORTED)

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "dda_no_side_effect_then_sendinput_failed"
    # The gated coordinate retry WAS attempted (eligible + side-effect-free);
    # it just failed to land. Centre of (100,100)-(140,130) is (120, 115).
    assert coord_calls == [(120, 115)]


def test_dda_no_side_effect_not_eligible_fails_closed_no_coord():
    # Side-effect-free HRESULT from DoDefaultAction BUT a bare substring+role
    # mismatch (fails the stronger eligibility gate) -> fail closed, no coord.
    control = FakeControl()
    coord_calls: list[Any] = []

    def dda(_ref: Any) -> None:
        raise FakeComError(UIA_E_ELEMENTNOTAVAILABLE)

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
    )
    result = ex.click(
        make_match(control, name="Welcome cancel screen", role="text"),
        snap(),
        QUERY,
    )
    assert result.outcome == "execution_failed"
    # The stronger eligibility gate blocked the coordinate click; the press
    # failed, so it is reported as a failed default-action press.
    assert result.reason == "dda_no_default_action_failed"
    assert coord_calls == []


def test_dda_non_success_hresult_not_allowlisted_fails_closed_HONESTY():
    # THE HONESTY TEST. DoDefaultAction returns a non-success HRESULT that is
    # NOT on the no-side-effect allowlist. accDoDefaultAction may have FIRED the
    # action before returning the error, so the executor must NEVER assume the
    # press succeeded and must NEVER coordinate-click (a double-fire). Fail
    # closed and surface a notice -- even with the operator knob True AND an
    # eligible exact-name match (the most permissive config).
    control = FakeControl()
    coord_calls: list[Any] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    def dda(_ref: Any) -> None:
        raise FakeComError(NON_ALLOWLISTED_HRESULT)

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        enable_coordinate_click_on_com_error=True,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    # A non-success, non-allowlisted DoDefaultAction return: never assume the
    # press happened, never coordinate-click. Surfaced as a failed default
    # action press (the notice path keys off this reason).
    assert result.reason == "dda_no_default_action_failed"
    assert coord_calls == []  # the double-fire is prevented


def test_dda_non_com_exception_fails_closed_no_coord():
    # A NON-COM exception from the DoDefaultAction seam (carrying a stray
    # allowlisted .hresult) must NOT be classified side-effect-free -- mirrors
    # the Invoke-side reviewer_1 finding. Fail closed, no coordinate click.
    class NotAComError(Exception):
        def __init__(self, hresult: int) -> None:
            super().__init__("not a real COM error")
            self.hresult = hresult

    control = FakeControl()
    coord_calls: list[Any] = []

    def dda(_ref: Any) -> None:
        raise NotAComError(UIA_E_NOTSUPPORTED)

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        enable_coordinate_click_on_com_error=True,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "dda_no_default_action_failed"
    assert coord_calls == []


def test_dda_never_attempted_when_invoke_available_com_error():
    # When InvokePattern is AVAILABLE but Invoke raises a COM error, the
    # DoDefaultAction seam must NEVER be consulted -- the existing fail-closed
    # InvokePattern handling is preserved untouched.
    control = FakeControl(invoke_raises=FakeComError(NON_ALLOWLISTED_HRESULT))
    dda_calls: list[Any] = []

    def dda(ref: Any) -> None:
        dda_calls.append(ref)
        return None

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
        do_default_action_fn=dda,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "invoke_com_error"
    assert dda_calls == []  # the DDA fallback was never reached


def test_dda_default_real_press_no_legacy_pattern_is_unavailable():
    # When no do_default_action_fn is injected, the real press default
    # (do_default_action_via_legacy_pattern) runs. FakeControl exposes no
    # GetCurrentPattern, so the press resolves to no Legacy pattern and raises
    # DoDefaultActionUnavailable, mapping an InvokePatternUnavailable Invoke
    # failure to dda_unavailable. It never silently "succeeds". The match is
    # INELIGIBLE for the coordinate gate (bare substring name, role mismatch)
    # so the structural coordinate fallback stays out of the way -- this
    # executor has NO injected coordinate seam, and an eligible match would
    # hit the constructor's RAISING placeholders (window_at_point_fn first),
    # producing a spurious click_point_obstructed instead of the plain
    # dda_unavailable this test pins. Real input is impossible either way:
    # the placeholder raise, not match ineligibility, is the backstop
    # (wh-explorer-navpane-click.1.3).
    control = FakeControl()
    ex = ClickExecutor(
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
    )
    result = ex.click(
        make_match(control, name="Please cancel now", role="text"), snap(), QUERY
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "dda_unavailable"


# ---------------------------------------------------------------------------
# Structural-absence coordinate fallback (wh-explorer-navpane-click).
#
# When InvokePattern is structurally unavailable AND the MSAA DoDefaultAction
# path is structurally absent (no Legacy pattern -> dda_unavailable, or the
# pattern's DefaultAction is empty -> dda_no_default_action), NOTHING has
# fired: NoDefaultAction is raised BEFORE accDoDefaultAction is called, and
# DoDefaultActionUnavailable means the pattern never resolved. Both states are
# provably side-effect-free -- safer than the allowlisted-HRESULT branch that
# already coordinate-clicks. So the executor falls through to the SAME gated
# coordinate fallback (same _coord_eligible gate, same _coordinate_fallback
# re-verification), with NO knob: enable_coordinate_click_on_com_error covers
# unproven side-effect states, which these are not.
#
# Live motivation: every File Explorer navigation-pane folder exposes no UIA
# Invoke pattern, and the pinned Quick Access items expose an EMPTY MSAA
# default action, so "click N" on them always failed with the permanent
# notice. SelectionItem.Select() was verified live to move the tree highlight
# WITHOUT navigating (Win11 WinUI tree), so a real coordinate click is the
# only honest press path left.
# ---------------------------------------------------------------------------


def test_dda_unavailable_eligible_falls_through_to_coordinate_click(caplog):
    # Invoke structurally unavailable, Legacy pattern absent, match passes the
    # stronger eligibility gate (exact name) -> coordinate click fires and
    # succeeds, knob False (the new path is knob-free by design). Telemetry
    # tag dda_unavailable_then_coord on the path.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    def dda(_ref: Any) -> None:
        raise DoDefaultActionUnavailable("no LegacyIAccessible pattern")

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
        enable_coordinate_click_on_com_error=False,
    )
    with caplog.at_level(logging.INFO, logger="ui.click_executor"):
        result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.clicked_via == "coordinate"
    assert len(coord_calls) == 1
    assert "dda_unavailable_then_coord" in caplog.text


def test_dda_no_default_action_eligible_falls_through_to_coordinate_click(caplog):
    # Invoke structurally unavailable, Legacy pattern present but its default
    # action is EMPTY (raised before accDoDefaultAction fires, so nothing
    # happened), match eligible -> coordinate click fires and succeeds with
    # the knob False. Telemetry tag dda_no_default_action_then_coord.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    def dda(_ref: Any) -> None:
        raise NoDefaultAction("control exposes no MSAA default action")

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
        enable_coordinate_click_on_com_error=False,
    )
    with caplog.at_level(logging.INFO, logger="ui.click_executor"):
        result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.clicked_via == "coordinate"
    assert len(coord_calls) == 1
    assert "dda_no_default_action_then_coord" in caplog.text


def test_dda_structural_coordinate_failure_reports_distinct_reason():
    # The structural fallback's coordinate click does not land (SendInput
    # reports failure) -> the reason names the WHOLE chain, distinct from both
    # the plain structural reason and the honesty-boundary reason.
    control = FakeControl()

    def coord(_x: int, _y: int) -> tuple[bool, int]:
        return (False, 2)

    def dda(_ref: Any) -> None:
        raise NoDefaultAction("control exposes no MSAA default action")

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
        enable_coordinate_click_on_com_error=False,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "dda_no_default_action_then_sendinput_failed"


def test_dda_structural_short_send_reports_sendinput_short():
    # A short send inside the structural fallback stays its own reason
    # (sendinput_short), exactly like every other _coordinate_fallback caller.
    control = FakeControl()

    def coord(_x: int, _y: int) -> tuple[bool, int]:
        return (True, 1)  # one event: the OS dropped half the click

    def dda(_ref: Any) -> None:
        raise DoDefaultActionUnavailable("no LegacyIAccessible pattern")

    ex = ClickExecutor(
        coordinate_click_fn=coord,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
        enable_coordinate_click_on_com_error=False,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "sendinput_short"


# ---------------------------------------------------------------------------
# Click-point hit-test (wh-explorer-navpane-click.1.1, reviewer_0 finding).
#
# _verify checks foreground identity and the control's own rectangle, but
# NONE of its five steps can detect an always-on-top window overlapping the
# target that does not own foreground (a PiP player, an on-top utility). The
# numbered badges paint on TOPMOST click-through windows, so badge N renders
# ABOVE such an occluder while the real click would land IN it. Before
# sending, _coordinate_fallback therefore hit-tests the click point through
# the window_at_point_fn seam (production: WindowFromPoint -> GetAncestor
# GA_ROOT) and requires the root window at the point to be the winner's own
# top-level window: winner.source_window_hwnd for a popup-owned winner
# (menus are their own root), else the verified foreground window. Any
# mismatch, a seam failure, or the un-injected placeholder fails closed
# under click_point_obstructed with NO input sent.
# ---------------------------------------------------------------------------


def _structural_dda(_ref: Any) -> None:
    raise DoDefaultActionUnavailable("no LegacyIAccessible pattern")


def test_coordinate_click_refuses_when_other_root_at_click_point():
    # The hit-test reports a DIFFERENT top-level window under the click point
    # (an always-on-top occluder) -> fail closed click_point_obstructed, the
    # coordinate seam never fires.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 4242,  # not the foreground window
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []


def test_coordinate_click_proceeds_when_root_at_point_matches():
    # The hit-test reports the verified foreground window's root at the click
    # point -> the click proceeds. The hit-test receives the SAME fresh rect
    # centre the click uses: make_match bounds (100, 100, 40, 30) -> (120, 115).
    control = FakeControl()
    hit_calls: list[tuple[int, int]] = []

    def window_at_point(x: int, y: int) -> int:
        hit_calls.append((x, y))
        return 1000  # matching_probe()'s window

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert hit_calls == [(120, 115)]


def test_coordinate_click_hit_test_expects_popup_root_for_popup_winner():
    # A popup-owned winner (source_window_hwnd != 0) lives in its OWN root
    # window (a menu), so the hit-test must compare against the popup's
    # window, not the foreground window that owns it.
    import dataclasses

    control = FakeControl()
    match = dataclasses.replace(
        make_match(control, name="cancel"), source_window_hwnd=7777
    )
    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 7777,
        point_hits_winner_fn=lambda _w, _x, _y: True,
        popup_visible_fn=lambda _h: True,
        popup_owner_fn=lambda _h: 1000,
    )
    result = ex.click(match, snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"


def test_coordinate_click_hit_test_failure_fails_closed():
    # The hit-test seam raising means the executor cannot verify what is at
    # the click point -> fail closed, no input.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    def broken_hit_test(_x: int, _y: int) -> int:
        raise RuntimeError("WindowFromPoint failed")

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=broken_hit_test,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []


def test_coordinate_click_hit_test_placeholder_default_fails_closed():
    # No window_at_point_fn injected -> the raising placeholder default fails
    # closed exactly like the coordinate_click_fn placeholder: production
    # wiring can never silently skip the hit-test.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []


def test_no_default_action_with_chained_cause_still_coordinate_clicks(caplog):
    # wh-explorer-navpane-click.1.2 (reviewer_0): NoDefaultAction has a THIRD
    # raise site -- the DefaultAction READ itself failed (chained cause), a
    # transient-instability signal rather than a structural absence. The
    # nothing-fired guarantee holds there too (the read is a property get;
    # accDoDefaultAction is never reached), and a control with a broken MSAA
    # implementation is squarely in the motivating class (no Invoke, no
    # usable MSAA), so the executor DELIBERATELY treats it the same: the
    # full re-verification plus the click-point hit-test stand in front of
    # the click. This test pins that decision.
    control = FakeControl()

    def dda_read_failure(_ref: Any) -> None:
        try:
            raise ValueError("CurrentDefaultAction read failed")
        except ValueError as exc:
            raise NoDefaultAction(
                "LegacyIAccessible pattern exposes no default action to perform"
            ) from exc

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda_read_failure,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )
    with caplog.at_level(logging.INFO, logger="ui.click_executor"):
        result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert "dda_no_default_action_then_coord" in caplog.text


# ---------------------------------------------------------------------------
# UIA point-hits-winner check (wh-explorer-navpane-click.1.4)
#
# The root-window hit-test above cannot see a SAME-ROOT occluder: an in-window
# overlay (a Chromium in-page modal, a same-process floating panel) shares the
# expected top-level window, so WindowFromPoint -> GetAncestor(GA_ROOT) passes.
# A second seam asks UI Automation which ELEMENT is at the click point and
# requires it to resolve to the winner itself, a descendant of it, or one of
# its containers (weak accessibility implementations report coarse elements).
# Same placeholder discipline as the other two coordinate seams: the default
# raises, and any raise or False refuses under click_point_obstructed.
# ---------------------------------------------------------------------------


def test_coordinate_click_refuses_when_point_misses_winner_subtree():
    # UIA reports an element at the click point that is unrelated to the
    # winner (a same-root occluder) -> fail closed click_point_obstructed,
    # the coordinate seam never fires.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: False,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []


def test_coordinate_click_proceeds_when_point_hits_winner():
    # The UIA check confirms the element at the point is the winner (or in
    # its subtree) -> the click proceeds. The check receives the winner match
    # and the SAME fresh rect centre the click uses: (120, 115).
    control = FakeControl()
    hit_args: list[tuple[object, int, int]] = []

    def point_hits_winner(winner, x: int, y: int) -> bool:
        hit_args.append((winner, x, y))
        return True

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=point_hits_winner,
    )
    match = make_match(control, name="cancel")
    result = ex.click(match, snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert hit_args == [(match, 120, 115)]


def test_coordinate_click_uia_hit_check_failure_fails_closed():
    # The UIA check raising (ElementFromPoint COM error) means the executor
    # cannot verify what is at the click point -> fail closed, no input.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    def broken_check(_w, _x: int, _y: int) -> bool:
        raise RuntimeError("ElementFromPoint failed")

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=broken_check,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []


def test_coordinate_click_uia_hit_check_placeholder_default_fails_closed():
    # window_at_point_fn injected but NO point_hits_winner_fn -> the raising
    # placeholder default fails closed: production wiring can never silently
    # skip the UIA-level check either.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 1000,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []


def test_uia_hit_check_runs_only_after_root_check_passes():
    # Ordering pin: a root-window mismatch refuses BEFORE the UIA check runs
    # (the cheap Win32 comparison short-circuits the COM call).
    control = FakeControl()
    uia_calls: list[tuple[int, int]] = []

    def point_hits_winner(_w, x: int, y: int) -> bool:
        uia_calls.append((x, y))
        return True

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=_structural_dda,
        window_at_point_fn=lambda _x, _y: 4242,  # root mismatch
        point_hits_winner_fn=point_hits_winner,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert uia_calls == []


# ---------------------------------------------------------------------------
# Badge coordinate-first (wh-electron-dda-noop).
#
# A numbered-badge pick is literally "click the thing at that spot", and MSAA
# accDoDefaultAction can return success without firing anything (live case:
# the Claude Desktop Electron sidebar button 'More options for Shared beads
# server' -- dda_ok reported, no menu opened). So for badge picks ONLY
# (click(..., badge_pick=True)), when InvokePattern is structurally
# unavailable the executor attempts the guarded coordinate click BEFORE
# DoDefaultAction. Both hit-test layers and the full re-verification stay.
# DoDefaultAction remains the fallback -- but ONLY when the coordinate
# attempt provably sent no input on a still-verified target (hit-test
# refusal or SendInput reporting zero events). A re-verification FAILURE
# surfaces its own reason with no DDA press (wh-review-click-numbers.3: the
# world moved, so DDA would press the stale control_ref verification just
# rejected); after a partial or ambiguous send a DDA press could
# double-fire, so those fail closed exactly as before.
# By-name clicks (badge_pick absent/False) keep the DDA-first order.
# ---------------------------------------------------------------------------


def _badge_executor(
    *,
    dda_calls: list,
    coordinate_click=None,
    window_at_point=None,
    on_screen=always_on_screen,
) -> ClickExecutor:
    """Executor with a SUCCEEDING DoDefaultAction seam that records calls.

    Models the Electron lie: if DDA runs, it reports success (returns
    normally) while firing nothing -- so any test asserting the click went
    through the coordinate path proves DDA was never consulted.
    """

    def dda(ref: Any) -> None:
        dda_calls.append(ref)
        return None  # "success" -- the lie

    if coordinate_click is None:
        coordinate_click = lambda _x, _y: (True, 2)
    if window_at_point is None:
        window_at_point = lambda _x, _y: 1000
    return ClickExecutor(
        coordinate_click_fn=coordinate_click,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )


def test_badge_pick_coordinate_clicks_before_dda():
    # THE wh-electron-dda-noop pin: Invoke structurally unavailable on a badge
    # pick -> the guarded coordinate click runs INSTEAD of DoDefaultAction.
    # Without badge-first ordering, the lying DDA seam would return "ok" via
    # invoke and the coordinate seam would never fire.
    control = FakeControl()
    dda_calls: list[Any] = []
    coord_calls: list[tuple[int, int]] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert coord_calls == [(120, 115)]
    assert dda_calls == []


def test_by_name_click_keeps_dda_first_order():
    # badge_pick defaults to False: the by-name path is untouched -- DDA runs
    # first and its success is trusted, the coordinate seam never fires.
    control = FakeControl()
    dda_calls: list[Any] = []
    coord_calls: list[tuple[int, int]] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert len(dda_calls) == 1
    assert coord_calls == []


def test_badge_pick_invoke_success_never_reaches_fallbacks():
    # A badge pick with a working InvokePattern is the plain happy path.
    control = FakeControl()
    dda_calls: list[Any] = []
    coord_calls: list[tuple[int, int]] = []

    def dda(ref: Any) -> None:
        dda_calls.append(ref)

    ex = ClickExecutor(
        coordinate_click_fn=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
        do_default_action_fn=dda,
        window_at_point_fn=lambda _x, _y: 1000,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1
    assert dda_calls == []
    assert coord_calls == []


def test_badge_pick_obstructed_point_falls_back_to_dda():
    # Hit-test refusal (root-window mismatch) happens BEFORE any input is
    # sent, so falling back to DoDefaultAction cannot double-fire. The DDA
    # seam succeeds -> ok via invoke, coordinate seam never sent anything.
    control = FakeControl()
    dda_calls: list[Any] = []
    coord_calls: list[tuple[int, int]] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        window_at_point=lambda _x, _y: 4242,  # occluder root at the point
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert len(dda_calls) == 1
    assert coord_calls == []


def test_badge_pick_reverify_failure_fails_with_its_reason():
    # wh-review-click-numbers.3: a re-verification failure means the WORLD
    # MOVED between the Invoke attempt and the fallback -- the winner may be
    # disabled, offscreen, or under a different foreground. Pressing the
    # stale control_ref via DDA would act on a target verification just
    # rejected, so the refusal must surface the verification reason and
    # never divert to the DDA fallback.
    control = FakeControl()
    dda_calls: list[Any] = []
    coord_calls: list[tuple[int, int]] = []
    seen: list[tuple[int, int]] = []

    def on_screen_once(x: int, y: int) -> bool:
        seen.append((x, y))
        return len(seen) == 1  # first verify passes, re-verify fails

    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        on_screen=on_screen_once,
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "execution_failed"
    assert result.reason == "target_moved_offscreen"
    assert dda_calls == []
    assert coord_calls == []


def test_badge_pick_zero_event_send_failure_falls_back_to_dda():
    # The production seam returns (False, 0) when the cursor landed wrong
    # BEFORE any button event was synthesised -- provably nothing fired, so
    # the DDA fallback is safe.
    control = FakeControl()
    dda_calls: list[Any] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda _x, _y: (False, 0),
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert len(dda_calls) == 1


def test_badge_pick_zero_event_fallback_reason_is_nothing_sent(caplog):
    # The no_input_fallback hook receives the specific zero-event refusal
    # tag, not the generic badge_coord_first_sendinput_failed fail_reason --
    # that string would falsely log that a send was attempted when the seam
    # provably injected nothing (wh-review-click-numbers.2 part B).
    control = FakeControl()
    dda_calls: list[Any] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda _x, _y: (False, 0),
    )
    with caplog.at_level(logging.INFO):
        result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                          badge_pick=True)
    assert result.outcome == "ok"
    assert "(sendinput_nothing_sent)" in caplog.text
    assert "(badge_coord_first_sendinput_failed)" not in caplog.text


def test_badge_pick_partial_send_fails_closed_without_dda():
    # One of the two button events went out: the click may have half-fired.
    # A DDA press on top could double-fire -> fail closed under the existing
    # sendinput_short reason, DDA never consulted.
    control = FakeControl()
    dda_calls: list[Any] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda _x, _y: (False, 1),
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "execution_failed"
    assert result.reason == "sendinput_short"
    assert dda_calls == []


def test_badge_pick_coord_seam_raise_fails_closed_without_dda():
    # A raising coordinate seam is ambiguous -- input may or may not have
    # gone out mid-call -- so it can never fall back to a DDA press.
    control = FakeControl()
    dda_calls: list[Any] = []

    def broken_coord(_x: int, _y: int) -> tuple[bool, int]:
        raise RuntimeError("SendInput blew up")

    ex = _badge_executor(dda_calls=dda_calls, coordinate_click=broken_coord)
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY,
                      badge_pick=True)
    assert result.outcome == "execution_failed"
    assert result.reason == "badge_coord_first_sendinput_failed"
    assert dda_calls == []


def test_badge_pick_ineligible_match_goes_straight_to_dda():
    # A match failing the stronger coordinate eligibility gate (bare
    # substring name, role mismatch) never coordinate-clicks; the badge path
    # then behaves exactly like today: DDA first.
    control = FakeControl()
    dda_calls: list[Any] = []
    coord_calls: list[tuple[int, int]] = []
    ex = _badge_executor(
        dda_calls=dda_calls,
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(
        make_match(control, name="Please cancel now", role="text"),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert len(dda_calls) == 1
    assert coord_calls == []


# ---------------------------------------------------------------------------
# Shell-owned badge coordinate-first (wh-tray-invoke-noop).
#
# The Win11 taskbar tray accepts a UIA Invoke on a tray icon, RETURNS
# SUCCESS, and only moves keyboard focus -- the tray app never sees a click
# (live case: the Hot Virtual Keyboard icon drew a focus rectangle and never
# opened). Structurally the Invoke pattern is present, so the
# InvokePatternUnavailable reorder above never engages. For badge picks whose
# winner is shell-owned (ElementMatch.source_window_is_shell,
# wh-overlay-taskbar-numbers) the executor therefore goes coordinate-first
# even though Invoke exists: guarded coordinate click, with the normal
# Invoke path as the fallback ONLY when the coordinate attempt provably sent
# no input. Non-shell badge picks and all by-name clicks keep Invoke-first.
# ---------------------------------------------------------------------------


def _shell_executor(
    *,
    coordinate_click=None,
    window_at_point=None,
    invoke_fn=None,
    on_screen=always_on_screen,
) -> ClickExecutor:
    """Executor whose (fake) Invoke succeeds, for shell badge-first tests."""
    if coordinate_click is None:
        coordinate_click = lambda _x, _y: (True, 2)
    if window_at_point is None:
        window_at_point = lambda _x, _y: 1000
    if invoke_fn is None:
        invoke_fn = lambda ref: ref.Invoke()
    return ClickExecutor(
        coordinate_click_fn=coordinate_click,
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=invoke_fn,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )


def test_shell_badge_pick_coordinate_clicks_before_invoke():
    # THE wh-tray-invoke-noop pin: a shell-owned badge pick coordinate-clicks
    # FIRST -- the control's working Invoke pattern (which would lie) is
    # never consulted.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []
    ex = _shell_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert coord_calls == [(120, 115)]
    assert control.invoke_calls == 0


def test_nonshell_badge_pick_keeps_invoke_first():
    # Option 2 (coordinate-first everywhere) was declined: a badge pick on an
    # ordinary in-window control still trusts its working Invoke pattern.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []
    ex = _shell_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(
        make_match(control, name="cancel"), snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1
    assert coord_calls == []


def test_shell_by_name_click_keeps_invoke_first():
    # A by-name click (badge_pick False) on a shell-owned control is
    # untouched: Invoke first, coordinate seam never fires.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []
    ex = _shell_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1
    assert coord_calls == []


def test_shell_badge_pick_obstructed_falls_back_to_invoke():
    # A hit-test refusal sends no input, so falling back to the normal
    # Invoke path is safe -- and Invoke succeeds as today.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []
    ex = _shell_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        window_at_point=lambda _x, _y: 4242,  # occluder root at the point
    )
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1
    assert coord_calls == []


def test_shell_badge_pick_reverify_failure_fails_with_its_reason():
    # wh-review-click-numbers.3, shell twin: a re-verification failure inside
    # the coordinate-first attempt means the world moved -- the Invoke
    # fallback must NOT press the stale control_ref; the verification reason
    # surfaces instead.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []
    seen: list[tuple[int, int]] = []

    def on_screen_once(x: int, y: int) -> bool:
        seen.append((x, y))
        return len(seen) == 1  # first verify passes, re-verify fails

    ex = _shell_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        on_screen=on_screen_once,
    )
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "target_moved_offscreen"
    assert control.invoke_calls == 0
    assert coord_calls == []


def test_shell_badge_pick_zero_event_send_falls_back_to_invoke():
    # (False, 0) from the seam = the cursor landed wrong before any button
    # event went out -- provably nothing fired, Invoke fallback is safe.
    control = FakeControl()
    ex = _shell_executor(coordinate_click=lambda _x, _y: (False, 0))
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1


def test_shell_badge_pick_zero_event_fallback_reason_is_nothing_sent(caplog):
    # Shell sibling of the badge_coord_first case: the Invoke fallback's log
    # line names the specific zero-event refusal, not the generic send-failure
    # fail_reason (wh-review-click-numbers.2 part B).
    control = FakeControl()
    ex = _shell_executor(coordinate_click=lambda _x, _y: (False, 0))
    with caplog.at_level(logging.INFO):
        result = ex.click(
            make_match(control, name="cancel", source_window_is_shell=True),
            snap(), QUERY, badge_pick=True,
        )
    assert result.outcome == "ok"
    assert "(sendinput_nothing_sent)" in caplog.text
    assert "(badge_coord_first_sendinput_failed)" not in caplog.text


def test_shell_badge_pick_partial_send_fails_closed_without_invoke():
    # One button event out of two went out: the click may have half-fired.
    # An Invoke press on top could double-fire -> fail closed, Invoke never
    # consulted.
    control = FakeControl()
    ex = _shell_executor(coordinate_click=lambda _x, _y: (False, 1))
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "execution_failed"
    assert result.reason == "sendinput_short"
    assert control.invoke_calls == 0


def test_shell_badge_pick_ineligible_goes_straight_to_invoke():
    # An eligibility-gate failure routes to the normal Invoke path exactly
    # as today, with no coordinate attempt.
    control = FakeControl()
    coord_calls: list[tuple[int, int]] = []
    ex = _shell_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )
    result = ex.click(
        make_match(control, name="Please cancel now", role="text",
                   source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1
    assert coord_calls == []


def test_shell_badge_pick_refusal_then_invoke_unavailable_reaches_dda():
    # Chain: coordinate refused with no input -> Invoke path -> Invoke
    # structurally unavailable -> DDA. The fallback deliberately does NOT
    # re-enter the badge coordinate-first reorder (the coordinate attempt
    # was just refused), so the hit-test runs exactly once.
    control = FakeControl()
    dda_calls: list[Any] = []
    hit_test_calls: list[tuple[int, int]] = []

    def dda(ref: Any) -> None:
        dda_calls.append(ref)

    def window_at_point(x: int, y: int) -> int:
        hit_test_calls.append((x, y))
        return 4242  # occluder root -> refusal, nothing sent

    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        do_default_action_fn=dda,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=lambda _w, _x, _y: True,
    )
    result = ex.click(
        make_match(control, name="cancel", source_window_is_shell=True),
        snap(), QUERY, badge_pick=True,
    )
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert len(dda_calls) == 1
    assert len(hit_test_calls) == 1


# ---------------------------------------------------------------------------
# Pre-click verification budget (wh-overlay-slow-uia-stale-badges.6).
#
# The observed defect: _verify blocked 7810 ms on COM/Win32 reads with no time
# limit, so Logic's 3000 ms awaiter gave up first and the true refusal was
# discarded. The executor now captures a monotonic deadline at click() entry
# and checks it BETWEEN verification steps; an expired budget refuses with
# ``verification_timeout`` and starts no further COM read and sends no input.
# The budget is cumulative per click() call: the _verify re-run inside
# _coordinate_fallback shares the deadline captured at click() entry.
# ---------------------------------------------------------------------------


class FakeClock:
    """A controllable monotonic clock for the budget seams."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TimedControl:
    """A fake control_ref whose IsEnabled read can consume budget time.

    ``enabled_advance_s`` moves the fake clock forward when
    ``CurrentIsEnabled`` is read, modelling the incident's slow COM property
    reads. ``bounds_reads`` counts ``CurrentBoundingRectangle`` accesses so a
    test can prove no NEW COM read starts after the budget expires.
    """

    def __init__(self, clock: FakeClock, *, enabled_advance_s: float = 0.0) -> None:
        self._clock = clock
        self._enabled_advance_s = enabled_advance_s
        self.invoke_calls = 0
        self.bounds_reads = 0

    @property
    def CurrentIsEnabled(self) -> bool:
        self._clock.advance(self._enabled_advance_s)
        return True

    @property
    def CurrentBoundingRectangle(self) -> FakeRect:
        self.bounds_reads += 1
        return FakeRect(100, 100, 140, 130)

    def Invoke(self) -> None:
        self.invoke_calls += 1


def _budget_executor(
    clock: FakeClock,
    *,
    verification_budget_ms: int = 2000,
    foreground_probe=None,
    gesture_click=None,
    coordinate_click=None,
    on_screen=None,
    window_at_point=None,
    point_hits=None,
):
    """Build a ClickExecutor wired to the fake clock (budget test surface)."""
    if foreground_probe is None:
        foreground_probe = probe_fn(matching_probe())
    if coordinate_click is None:
        coordinate_click = lambda _x, _y: (True, 2)
    if on_screen is None:
        on_screen = always_on_screen
    if window_at_point is None:
        window_at_point = lambda _x, _y: 1000
    if point_hits is None:
        point_hits = lambda _w, _x, _y: True
    kwargs: dict[str, Any] = {}
    if gesture_click is not None:
        kwargs["gesture_click_fn"] = gesture_click
    return ClickExecutor(
        coordinate_click_fn=coordinate_click,
        foreground_probe=foreground_probe,
        on_screen_fn=on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=point_hits,
        verification_budget_ms=verification_budget_ms,
        monotonic_fn=clock,
        **kwargs,
    )


def test_budget_expiry_between_steps_refuses_and_starts_no_new_read():
    # The incident seam: the budget expires during the IsEnabled read (step 4),
    # so the check BEFORE the BoundingRectangle read (step 5) must refuse with
    # verification_timeout. The bounds read must never start, and no input of
    # any kind may be sent.
    clock = FakeClock()
    control = TimedControl(clock, enabled_advance_s=2.5)
    coord_calls: list[tuple[int, int]] = []

    def coord(x: int, y: int) -> tuple[bool, int]:
        coord_calls.append((x, y))
        return (True, 2)

    ex = _budget_executor(clock, coordinate_click=coord)
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "verification_timeout"
    assert result.matched_name == "Cancel"
    assert control.bounds_reads == 0
    assert control.invoke_calls == 0
    assert coord_calls == []


def test_budget_is_cumulative_across_coordinate_fallback_reverify():
    # The _verify re-run inside _coordinate_fallback must NOT reset the
    # deadline captured at click() entry. The gesture path runs _verify twice
    # (once in click(), once in the fallback); each foreground probe consumes
    # 1.5 s of the 2 s budget, so the FIRST run passes and the SECOND must
    # refuse with verification_timeout before any input is sent.
    clock = FakeClock()
    control = TimedControl(clock)
    base_probe = matching_probe()

    def advancing_probe() -> ForegroundProbe:
        clock.advance(1.5)
        return base_probe

    gesture_calls: list[tuple[int, int, str, int]] = []

    def gesture_click(x: int, y: int, button: str, count: int) -> tuple[bool, int]:
        gesture_calls.append((x, y, button, count))
        return (True, 2)

    from ui.element_types import ClickGesture

    query = ElementQuery(
        name="cancel", role="button", ordinal=None, spatial=None,
        raw_utterance="right click cancel", gesture=ClickGesture.RIGHT_CLICK,
    )
    ex = _budget_executor(
        clock, foreground_probe=advancing_probe, gesture_click=gesture_click,
    )
    result = ex.click(make_match(control), snap(), query)
    assert result.outcome == "execution_failed"
    assert result.reason == "verification_timeout"
    assert gesture_calls == []
    assert control.invoke_calls == 0


def test_click_well_inside_budget_is_unaffected():
    # A verification that never nears the deadline behaves exactly as before:
    # the click goes through Invoke and reports ok.
    clock = FakeClock()
    control = TimedControl(clock)
    ex = _budget_executor(clock)
    result = ex.click(make_match(control), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.reason is None
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1


class ArmedClock(FakeClock):
    """A FakeClock that applies a pending advance AFTER the next read.

    ``arm(seconds)`` schedules the advance; the NEXT ``__call__`` returns the
    current time and THEN moves the clock forward. Models time passing in the
    pure-Python tail after a budget check has already read the clock
    (wh-overlay-slow-uia-stale-badges.20.2).
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += self._pending
        self._pending = 0.0
        return value

    def arm(self, seconds: float) -> None:
        self._pending = seconds


def test_budget_expiry_during_hit_tests_sends_no_input():
    # wh-overlay-slow-uia-stale-badges.20.2: the deadline expires DURING the
    # coordinate tail's hit-tests (layer 2, point_hits_winner_fn, is a
    # cross-process COM call to the same slow provider the budget bounds).
    # The check between the hit-test layers and the input seam must refuse
    # with verification_timeout: the seam must never run, and the refusal
    # must NOT divert to any fallback press.
    clock = FakeClock()
    control = TimedControl(clock)
    gesture_calls: list[tuple[int, int, str, int]] = []

    def gesture_click(x: int, y: int, button: str, count: int) -> tuple[bool, int]:
        gesture_calls.append((x, y, button, count))
        return (True, 2)

    def slow_point_hits(_w: Any, _x: int, _y: int) -> bool:
        clock.advance(2.5)  # the COM call blocks past the 2000 ms budget
        return True

    from ui.element_types import ClickGesture

    query = ElementQuery(
        name="cancel", role="button", ordinal=None, spatial=None,
        raw_utterance="right click cancel", gesture=ClickGesture.RIGHT_CLICK,
    )
    ex = _budget_executor(
        clock, gesture_click=gesture_click, point_hits=slow_point_hits,
    )
    result = ex.click(make_match(control), snap(), query)
    assert result.outcome == "execution_failed"
    assert result.reason == "verification_timeout"
    assert gesture_calls == []
    assert control.invoke_calls == 0


def test_budget_expiry_at_reverify_return_stops_hit_tests():
    # wh-overlay-slow-uia-stale-badges.20.2, companion check: the budget
    # expires in the pure-Python tail of the fallback's _verify re-run (after
    # its last intra-step seam check). The check right after the re-verify
    # returns None must refuse BEFORE the hit-test layers start -- layer 2 is
    # a COM call that must not be given to an expired click.
    clock = ArmedClock()
    control = TimedControl(clock)
    on_screen_calls = [0]

    def counting_on_screen(_x: int, _y: int) -> bool:
        on_screen_calls[0] += 1
        if on_screen_calls[0] == 2:  # the fallback's re-verify run
            # Expire AFTER the on-screen seam check reads the clock, i.e. in
            # the pure-Python tail of _verify.
            clock.arm(2.5)
        return True

    window_calls: list[tuple[int, int]] = []

    def window_at_point(x: int, y: int) -> int:
        window_calls.append((x, y))
        return 1000

    hits_calls: list[int] = []

    def point_hits(_w: Any, _x: int, _y: int) -> bool:
        hits_calls.append(1)
        return True

    gesture_calls: list[tuple[int, int, str, int]] = []

    def gesture_click(x: int, y: int, button: str, count: int) -> tuple[bool, int]:
        gesture_calls.append((x, y, button, count))
        return (True, 2)

    from ui.element_types import ClickGesture

    query = ElementQuery(
        name="cancel", role="button", ordinal=None, spatial=None,
        raw_utterance="right click cancel", gesture=ClickGesture.RIGHT_CLICK,
    )
    ex = _budget_executor(
        clock, gesture_click=gesture_click, on_screen=counting_on_screen,
        window_at_point=window_at_point, point_hits=point_hits,
    )
    result = ex.click(make_match(control), snap(), query)
    assert result.outcome == "execution_failed"
    assert result.reason == "verification_timeout"
    assert window_calls == []
    assert hits_calls == []
    assert gesture_calls == []
    assert control.invoke_calls == 0


# ---------------------------------------------------------------------------
# Expand / Collapse default action (wh-treeitem-dda-wrong-action).
#
# Explorer's navigation-pane tree items ("This PC", "Desktop", the drives)
# expose no UIA Invoke pattern and an MSAA default action of "Expand" or
# "Collapse". Firing that default action toggles the node, while a mouse
# click navigates. So an Expand / Collapse default action must never be
# pressed: the executor takes the guarded coordinate click instead (the
# _coord_eligible gate, the full re-verification, and both click-point
# hit-test layers). A genuine default action (Press, Open, ...) still fires.
#
# These tests drive the REAL press (do_default_action_via_legacy_pattern, the
# constructor default) against a control that exposes the Legacy pattern the
# way a real element does: GetCurrentPattern -> QueryInterface -> the typed
# pattern with CurrentDefaultAction and DoDefaultAction.
# ---------------------------------------------------------------------------


class _FakeLegacyPattern:
    """The typed LegacyIAccessible pattern: a default action and a press."""

    def __init__(self, default_action: str) -> None:
        self.CurrentDefaultAction = default_action
        self.do_default_action_calls = 0

    def DoDefaultAction(self) -> None:
        self.do_default_action_calls += 1


class _FakeRawLegacyPattern:
    def __init__(self, typed: _FakeLegacyPattern) -> None:
        self._typed = typed

    def QueryInterface(self, _iface: Any) -> _FakeLegacyPattern:
        return self._typed


class _LegacyActionControl(FakeControl):
    """A control with no Invoke pattern whose Legacy pattern carries a
    default action (a nav-pane TreeItem when the action is Expand)."""

    def __init__(self, default_action: str) -> None:
        super().__init__()
        self.legacy = _FakeLegacyPattern(default_action)

    def GetCurrentPattern(self, _pattern_id: Any) -> _FakeRawLegacyPattern:
        return _FakeRawLegacyPattern(self.legacy)


UIA_TREEITEM_CONTROL_TYPE_ID = 50024

THIS_PC_QUERY = ElementQuery(
    name="this pc", role=None, ordinal=None, spatial=None,
    raw_utterance="click this pc",
)


def _tree_item_match(control: Any, *, name: str = "This PC") -> ElementMatch:
    import dataclasses

    return dataclasses.replace(
        make_match(control, name=name, role="tree item"),
        invoke_supported=False,
        control_type_id=UIA_TREEITEM_CONTROL_TYPE_ID,
    )


def _real_dda_executor(
    *,
    coordinate_click=None,
    window_at_point=None,
    point_hits_winner=None,
    foreground_probe=None,
) -> ClickExecutor:
    """An executor whose DoDefaultAction seam is the real production press."""
    if coordinate_click is None:
        coordinate_click = lambda _x, _y: (True, 2)
    if window_at_point is None:
        window_at_point = lambda _x, _y: 1000
    if point_hits_winner is None:
        point_hits_winner = lambda _w, _x, _y: True
    if foreground_probe is None:
        foreground_probe = probe_fn(matching_probe())
    return ClickExecutor(
        coordinate_click_fn=coordinate_click,
        foreground_probe=foreground_probe,
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=_raise_invoke_unavailable,
        window_at_point_fn=window_at_point,
        point_hits_winner_fn=point_hits_winner,
    )


@pytest.fixture
def _legacy_interface_resolves(monkeypatch):
    """Let the real press QueryInterface the fake raw pattern without needing
    the generated comtypes module."""
    from ui import uia_walker

    monkeypatch.setattr(uia_walker, "_legacy_pattern_class", lambda: object())


@pytest.mark.parametrize("default_action", ["Expand", "Collapse"])
def test_tree_item_expand_collapse_takes_coordinate_path_not_dda(
    _legacy_interface_resolves, caplog, default_action
):
    # THE wh-treeitem-dda-wrong-action pin: "click this pc" on an Explorer
    # nav-pane TreeItem whose only default action is Expand / Collapse must
    # click the node (navigate), not fire the default action (toggle).
    control = _LegacyActionControl(default_action)
    coord_calls: list[tuple[int, int]] = []
    ex = _real_dda_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )

    with caplog.at_level(logging.INFO, logger="ui.click_executor"):
        result = ex.click(_tree_item_match(control), snap(), THIS_PC_QUERY)

    assert result.outcome == "ok"
    assert result.clicked_via == "coordinate"
    assert coord_calls == [(120, 115)]
    assert control.legacy.do_default_action_calls == 0
    assert "dda_expand_collapse_then_coord" in caplog.text


@pytest.mark.parametrize(
    "default_action",
    [
        pytest.param("Press", id="press"),
        pytest.param("Open", id="open"),
        pytest.param("Double Click", id="double-click"),
    ],
)
def test_genuine_default_action_still_fires_dda(
    _legacy_interface_resolves, default_action
):
    # C2: a control whose default action is a genuine press keeps firing
    # DoDefaultAction unchanged; the coordinate seam never fires.
    control = _LegacyActionControl(default_action)
    coord_calls: list[tuple[int, int]] = []
    ex = _real_dda_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )

    result = ex.click(_tree_item_match(control), snap(), THIS_PC_QUERY)

    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.legacy.do_default_action_calls == 1
    assert coord_calls == []


def test_expand_collapse_ineligible_match_refuses_without_pressing(
    _legacy_interface_resolves,
):
    # The match fails the coordinate eligibility gate (the spoken name is
    # only a substring of the label and the query names no role): refuse
    # under dda_expand_collapse. Neither the default action nor a
    # coordinate click may fire.
    control = _LegacyActionControl("Expand")
    coord_calls: list[tuple[int, int]] = []
    ex = _real_dda_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
    )

    result = ex.click(
        _tree_item_match(control, name="Open This PC folder"), snap(),
        THIS_PC_QUERY,
    )

    assert result.outcome == "execution_failed"
    assert result.reason == "dda_expand_collapse"
    assert coord_calls == []
    assert control.legacy.do_default_action_calls == 0


def test_expand_collapse_coordinate_click_not_landing_reports_chain_reason(
    _legacy_interface_resolves,
):
    control = _LegacyActionControl("Expand")
    ex = _real_dda_executor(coordinate_click=lambda _x, _y: (False, 2))

    result = ex.click(_tree_item_match(control), snap(), THIS_PC_QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "dda_expand_collapse_then_sendinput_failed"
    assert control.legacy.do_default_action_calls == 0


@pytest.mark.parametrize(
    ("window_at_point", "point_hits_winner"),
    [
        (lambda _x, _y: 4242, lambda _w, _x, _y: True),  # other root window
        (lambda _x, _y: 1000, lambda _w, _x, _y: False),  # same-root occluder
    ],
    ids=["root_window_mismatch", "element_at_point_not_winner"],
)
def test_expand_collapse_click_point_check_refuses_without_input(
    _legacy_interface_resolves, window_at_point, point_hits_winner
):
    # Both click-point layers stand in front of the Expand / Collapse
    # coordinate click. A refusal sends nothing and does not fall back to the
    # toggling default action.
    control = _LegacyActionControl("Collapse")
    coord_calls: list[tuple[int, int]] = []
    ex = _real_dda_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        window_at_point=window_at_point,
        point_hits_winner=point_hits_winner,
    )

    result = ex.click(_tree_item_match(control), snap(), THIS_PC_QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "click_point_obstructed"
    assert coord_calls == []
    assert control.legacy.do_default_action_calls == 0


def test_expand_collapse_reverification_failure_refuses_without_input(
    _legacy_interface_resolves,
):
    # The foreground matches at the first verification and has changed by the
    # coordinate fallback's re-verification: refuse with the verification
    # reason, send nothing, and never press the default action.
    control = _LegacyActionControl("Expand")
    probes = [matching_probe(), matching_probe(window=2000)]

    def changing_probe() -> ForegroundProbe:
        return probes.pop(0) if len(probes) > 1 else probes[0]

    coord_calls: list[tuple[int, int]] = []
    ex = _real_dda_executor(
        coordinate_click=lambda x, y: (coord_calls.append((x, y)) or (True, 2)),
        foreground_probe=changing_probe,
    )

    result = ex.click(_tree_item_match(control), snap(), THIS_PC_QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "foreground_changed"
    assert coord_calls == []
    assert control.legacy.do_default_action_calls == 0
