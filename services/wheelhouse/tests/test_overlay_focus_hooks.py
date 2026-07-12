"""Unit tests for the Logic-side overlay focus-hook decision logic (wh-n29v.21).

These tests cover the PURE, Win32-free decision logic in
``services/wheelhouse/overlay_focus_hooks.py``:

  1. The debounce window (collapses rapid foreground changes within
     ``overlay_focus_debounce_ms``, default 250).
  2. The paint_generation supersession drop (a debounced callback whose
     ``(overlay_session_id, paint_generation)`` no longer matches the
     machine's current pair is dropped).
  3. The resume full-foreground-identity comparison (HWND + PID + process
     name + window creation time -- NOT IsWindow alone).
  4. The raw foreground / destroy event -> ``OverlayEvent`` mapping.

The raw ``SetWinEventHook`` registration + message pump is a thin seam in
``main.py``; it is NOT exercised here (no faked OS event loop). The pure
logic takes injected inputs so it is testable without any live Win32 call.
"""

from __future__ import annotations

import pytest

from services.wheelhouse.click_overlay_state import (
    OverlayEvent,
    OverlayEventKind,
)
from services.wheelhouse.overlay_focus_hooks import (
    DEFAULT_FOCUS_DEBOUNCE_MS,
    ForegroundIdentity,
    FocusChangeDebouncer,
    identity_matches,
    map_destroy_event,
    map_foreground_event,
)


# ---------------------------------------------------------------------------
# Debounce window.
# ---------------------------------------------------------------------------


def test_first_event_always_fires():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True


def test_second_event_within_window_is_coalesced():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    # 249 ms later: still inside the 250 ms window -> dropped.
    assert deb.should_fire(now_ms=1249.0) is False


def test_event_after_window_fires_again():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    # 250 ms later: at the window edge -> fires (>= boundary is allowed).
    assert deb.should_fire(now_ms=1250.0) is True


def test_rapid_burst_collapses_to_one_fire():
    deb = FocusChangeDebouncer(debounce_ms=250)
    fires = [
        deb.should_fire(now_ms=t)
        for t in (1000.0, 1050.0, 1100.0, 1200.0, 1249.0)
    ]
    # Only the first of a rapid burst inside one window fires.
    assert fires == [True, False, False, False, False]


def test_dropped_event_does_not_advance_the_window():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    # A dropped event at 1100 must NOT reset the window to 1100; the next
    # gate is still measured from the 1000 fire, so 1250 fires.
    assert deb.should_fire(now_ms=1100.0) is False
    assert deb.should_fire(now_ms=1250.0) is True


def test_zero_debounce_fires_every_time():
    deb = FocusChangeDebouncer(debounce_ms=0)
    assert deb.should_fire(now_ms=1000.0) is True
    assert deb.should_fire(now_ms=1000.0) is True
    assert deb.should_fire(now_ms=1000.1) is True


def test_reset_clears_the_window():
    deb = FocusChangeDebouncer(debounce_ms=250)
    assert deb.should_fire(now_ms=1000.0) is True
    deb.reset()
    # After reset the next event is treated as a first event again.
    assert deb.should_fire(now_ms=1001.0) is True


# ---------------------------------------------------------------------------
# Phase 1.5 parity: the Logic process builds its focus-change debouncer from
# the VALIDATED overlay config value (ClickConfig.overlay_focus_debounce_ms),
# not from an independent raw read of the [click] block, so Logic and Input
# can never disagree on the validated config (wh-n29v.66). The former raw
# reader (read_focus_debounce_ms) was retired with this fix: it had no
# remaining production caller and was a second, divergent reader of the same
# key. The validating ClickConfig reader (ui/click_config.py, wh-n29v.29)
# owns the [0, 5000] range, default 250, and the 0 = no-debounce rule, and is
# covered by tests/test_click_config.py.
# ---------------------------------------------------------------------------


def _debouncer_controller(*, click_raw):
    """Bind the real debouncer-builder onto a spec mock, with a real config.

    Mirrors the established MagicMock(spec=LogicController) bound-method
    pattern from test_voice_overlay_routing._make_controller: the method under
    test is bound from the real class; ``click_config`` is the real validated
    ClickConfig built from ``click_raw`` exactly as LogicController.__init__
    does. This exercises the production construction expression without driving
    the heavy full __init__.
    """
    from unittest.mock import MagicMock

    from main import LogicController
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    c._build_overlay_focus_debouncer = (
        LogicController._build_overlay_focus_debouncer.__get__(c)
    )
    c.click_config = ClickConfig.from_raw(click_raw)
    return c


def test_logic_debouncer_uses_validated_value_for_out_of_range_config():
    # The bug this slice closes: a raw overlay_focus_debounce_ms outside the
    # validated range [0, 5000] must produce the SAME debounce the validator
    # records (the 250 fallback), so Logic agrees with Input. The pre-fix code
    # read the raw block directly and built the debouncer with 5001.
    from ui.click_config import ClickConfig

    raw_block = {"overlay_focus_debounce_ms": 5001}
    # The validator records 250 for an out-of-range value (and disables the
    # overlay), so Logic must use 250 too -- never 5001.
    assert ClickConfig.from_raw(raw_block).overlay_focus_debounce_ms == 250

    c = _debouncer_controller(click_raw=raw_block)
    debouncer = c._build_overlay_focus_debouncer()
    assert debouncer.debounce_ms == 250


def test_logic_debouncer_passes_through_valid_value():
    c = _debouncer_controller(click_raw={"overlay_focus_debounce_ms": 400})
    assert c._build_overlay_focus_debouncer().debounce_ms == 400


def test_logic_debouncer_passes_through_zero_no_debounce():
    # 0 is a valid in-range value (no-debounce) and must pass through unchanged.
    c = _debouncer_controller(click_raw={"overlay_focus_debounce_ms": 0})
    assert c._build_overlay_focus_debouncer().debounce_ms == 0


def test_logic_debouncer_default_when_key_absent():
    c = _debouncer_controller(click_raw={})
    assert (
        c._build_overlay_focus_debouncer().debounce_ms
        == DEFAULT_FOCUS_DEBOUNCE_MS
        == 250
    )


# ---------------------------------------------------------------------------
# Resume full-foreground-identity comparison.
# ---------------------------------------------------------------------------


def _ident(hwnd=111, pid=222, name="app.exe", created=999) -> ForegroundIdentity:
    return ForegroundIdentity(
        hwnd=hwnd, pid=pid, process_name=name, window_creation_time=created
    )


def test_identity_matches_when_all_fields_equal():
    assert identity_matches(_ident(), _ident()) is True


def test_identity_mismatch_on_hwnd():
    assert identity_matches(_ident(), _ident(hwnd=112)) is False


def test_identity_mismatch_on_pid():
    assert identity_matches(_ident(), _ident(pid=223)) is False


def test_identity_mismatch_on_process_name():
    assert identity_matches(_ident(), _ident(name="other.exe")) is False


def test_identity_mismatch_on_creation_time():
    # The HWND-reuse trap: same HWND + PID, different creation time -> mismatch.
    assert identity_matches(_ident(), _ident(created=1000)) is False


def test_identity_reused_hwnd_on_different_process_is_mismatch():
    # A recycled HWND landing on a different process must NOT match (the r2.8
    # case IsWindow alone would miss).
    captured = _ident(hwnd=500, pid=10, name="brave.exe", created=100)
    current = _ident(hwnd=500, pid=99, name="explorer.exe", created=200)
    assert identity_matches(captured, current) is False


# ---------------------------------------------------------------------------
# Raw-event -> OverlayEvent mapping.
# ---------------------------------------------------------------------------


def test_map_foreground_event_produces_focus_change():
    ev = map_foreground_event(hwnd=4242)
    assert isinstance(ev, OverlayEvent)
    assert ev.kind is OverlayEventKind.FOCUS_CHANGE


def test_map_destroy_event_matches_tracked_hwnd():
    ev = map_destroy_event(destroyed_hwnd=777, tracked_hwnd=777)
    assert ev is not None
    assert ev.kind is OverlayEventKind.FOCUSED_HWND_DESTROYED


def test_map_destroy_event_ignores_other_hwnd():
    # The destroy hook is filtered to the tracked window's pid/tid, but a
    # different HWND in that process must NOT drive the paused->closed edge.
    assert map_destroy_event(destroyed_hwnd=778, tracked_hwnd=777) is None


def test_map_destroy_event_ignores_zero_tracked_hwnd():
    # No tracked window -> never produce a destroy event.
    assert map_destroy_event(destroyed_hwnd=777, tracked_hwnd=0) is None


# ---------------------------------------------------------------------------
# Logic wiring: the LogicController focus-hook callbacks consult the pure
# logic and feed the machine. The Win32 manager + clock are injected; the
# state machine is real and asserted as data.
#
# NOTE (wh-n29v.22.1): the destroy-callback and resume-check tests below set
# ``_overlay_tracked_identity`` ON THE FIXTURE by hand. In production this slice
# (wh-n29v.21) never sets that field -- the effect-performing overlay
# integration (wh-h9a8v2) captures it when the overlay becomes visible/paused.
# These tests therefore exercise the destroy/resume wiring against an input the
# running system does not produce until that integration lands; they verify the
# wiring is correct, not that the feature is end-to-end live in this slice.
# ---------------------------------------------------------------------------


def _make_controller(*, debounce_ms: int = 250, enabled: bool = True):
    """Bind the real focus-hook controller methods onto a spec mock.

    Mirrors test_voice_overlay_routing._make_controller: the methods under
    test are bound from the real class; the state machine, config, debouncer,
    and tracked identity are real / simple fakes. The Win32 hook manager is a
    MagicMock so register/unregister are observable without a live hook.
    """
    from unittest.mock import MagicMock

    from main import LogicController, OverlayFocusHookManager
    from services.wheelhouse.click_overlay_state import ClickOverlayStateMachine
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer
    from ui.click_config import ClickConfig

    c = MagicMock(spec=LogicController)
    for name in (
        "_on_overlay_foreground_change",
        "_on_overlay_focused_hwnd_destroyed",
        "_apply_overlay_event",
        "_reconcile_overlay_destroy_hook",
        "overlay_snapshot_is_valid_on_resume",
        "_mint_overlay_trace_id",
    ):
        setattr(c, name, getattr(LogicController, name).__get__(c))

    c.click_config = ClickConfig.from_raw({"enabled": enabled})
    c.click_overlay_state = ClickOverlayStateMachine()
    c._overlay_focus_debouncer = FocusChangeDebouncer(debounce_ms=debounce_ms)
    c._overlay_tracked_identity = None
    c._overlay_destroy_hook_active = False
    c._overlay_focus_hooks = MagicMock(spec=OverlayFocusHookManager)
    c._perform_overlay_effects = MagicMock()
    return c


def test_foreground_callback_feeds_focus_change_when_painted():
    from services.wheelhouse.click_overlay_state import (
        OverlayEvent,
        OverlayEventKind,
        OverlayState,
        PaintAckState,
    )

    c = _make_controller()
    m = c.click_overlay_state
    # Drive to painted so FOCUS_CHANGE refreshes (a visible transition).
    m.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = m.overlay_session_id, m.paint_generation
    m.apply(OverlayEvent(OverlayEventKind.BUILD_RESPONSE,
                         overlay_session_id=sess, paint_generation=gen,
                         snapshot_id="snap"))
    m.apply(OverlayEvent(OverlayEventKind.PAINT_ACK,
                         overlay_session_id=sess, paint_generation=gen,
                         paint_state=PaintAckState.PAINTED))
    assert m.state is OverlayState.PAINTED

    c._on_overlay_foreground_change(4242)
    # A real focus change in painted drives a refresh.
    assert m.state is OverlayState.REFRESH_IN_FLIGHT
    c._perform_overlay_effects.assert_called_once()


def test_foreground_callback_coalesced_event_does_not_touch_machine():
    c = _make_controller(debounce_ms=10_000)
    # Prime the debounce window with one fire (in closed -> NO_OP, no effects).
    c._on_overlay_foreground_change(1)
    # wh-n29v.23.2: a closed-state NO_OP must NOT reset the debounce anchor.
    # (The reset belongs only to the transition INTO closed -- a session end.)
    # Without this guard the bug would clear the anchor here and the second
    # event below would fire as a first event instead of being coalesced.
    assert c._overlay_focus_debouncer._last_fired_ms is not None
    c._perform_overlay_effects.reset_mock()
    before = c.click_overlay_state.state
    # Second event within the window is coalesced: machine untouched.
    c._on_overlay_foreground_change(2)
    assert c.click_overlay_state.state is before
    c._perform_overlay_effects.assert_not_called()


def test_foreground_callback_gated_on_disabled_config():
    c = _make_controller(enabled=False)
    before = c.click_overlay_state.state
    c._on_overlay_foreground_change(4242)
    assert c.click_overlay_state.state is before
    c._perform_overlay_effects.assert_not_called()


def test_destroy_callback_ignored_when_hwnd_not_tracked():
    from services.wheelhouse.click_overlay_state import OverlayState

    c = _make_controller()
    c.click_overlay_state.state = OverlayState.PAUSED
    c._overlay_tracked_identity = ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )
    # The hook is live (machine is paused), so the callback reaches the
    # hwnd-narrowing check rather than the hook-inactive guard (wh-n29v.24.2).
    c._overlay_destroy_hook_active = True
    c._on_overlay_focused_hwnd_destroyed(778)  # different hwnd
    assert c.click_overlay_state.state is OverlayState.PAUSED


def test_destroy_callback_dropped_when_hook_inactive():
    # wh-n29v.24.2: the transient destroy hook is unregistered asynchronously
    # (PostMessage on the hook thread's ~20 ms poll). A destroy firing in the
    # gap between leaving paused and the worker thread processing the unregister
    # would otherwise feed FOCUSED_HWND_DESTROYED into a non-paused machine ->
    # INVALID_TRANSITION -> ERROR. The integration must drop a destroy callback
    # once the hook has been logically unregistered (_overlay_destroy_hook_active
    # is False): the hook is live only while paused, so any later callback is a
    # stale artifact of the unregister gap.
    from services.wheelhouse.click_overlay_state import OverlayState

    c = _make_controller()
    m = c.click_overlay_state
    # The machine has already left paused (e.g. a "hide numbers" closed it); the
    # tracked identity has not yet been cleared and a late destroy arrives.
    m.state = OverlayState.CLOSED
    c._overlay_tracked_identity = ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )
    c._overlay_destroy_hook_active = False
    c._on_overlay_focused_hwnd_destroyed(777)  # the tracked window, but stale
    # The stale destroy must be dropped, NOT drive the machine to ERROR.
    assert m.state is OverlayState.CLOSED


def test_destroy_callback_drives_paused_to_closed_for_tracked_hwnd():
    from services.wheelhouse.click_overlay_state import OverlayState

    c = _make_controller()
    m = c.click_overlay_state
    m.state = OverlayState.PAUSED
    m.pinned_snapshot_id = "snap"
    c._overlay_tracked_identity = ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )
    c._overlay_destroy_hook_active = True
    c._on_overlay_focused_hwnd_destroyed(777)  # the tracked window
    assert m.state is OverlayState.CLOSED
    # Leaving paused unregisters the transient destroy hook (no leaked hook).
    c._overlay_focus_hooks.unregister_destroy_hook.assert_called_once()
    assert c._overlay_destroy_hook_active is False


def test_session_end_resets_focus_debouncer():
    # wh-n29v.22.2: an event that drives the machine to closed clears the
    # debounce anchor, so the first focus change of a session opened right after
    # is not coalesced against this session's stale anchor.
    from services.wheelhouse.click_overlay_state import OverlayState

    c = _make_controller(debounce_ms=250)
    m = c.click_overlay_state
    m.state = OverlayState.PAUSED
    m.pinned_snapshot_id = "snap"
    c._overlay_tracked_identity = ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )
    c._overlay_destroy_hook_active = True
    # Prime the debounce anchor as if a focus change had fired this session.
    c._overlay_focus_debouncer._last_fired_ms = 5000.0
    # The tracked window is destroyed -> paused -> closed (session end).
    c._on_overlay_focused_hwnd_destroyed(777)
    assert m.state is OverlayState.CLOSED
    # 100 ms after the primed anchor: WITHOUT the session-end reset this would be
    # coalesced (False); the reset clears the anchor so it fires as a first event.
    assert c._overlay_focus_debouncer.should_fire(now_ms=5100.0) is True


def test_closed_state_foreground_change_mints_no_trace_id():
    # wh-n29v.22.3: a foreground change while the machine is closed is a NO_OP.
    # It must NOT mint a trace id or perform effects, so ordinary all-day window
    # switching does not churn uuids or spew INFO lines.
    from unittest.mock import MagicMock

    c = _make_controller()
    c._mint_overlay_trace_id = MagicMock(return_value="click-deadbeef")
    c._on_overlay_foreground_change(4242)  # machine starts closed -> NO_OP
    c._mint_overlay_trace_id.assert_not_called()
    c._perform_overlay_effects.assert_not_called()


def test_destroy_hook_active_flag_only_set_when_register_accepted():
    # wh-n29v.23.3: _reconcile records the destroy hook active ONLY when the
    # manager accepts the register request. A rejected post (manager dead,
    # PostMessage failed) must leave the flag False so the next paused reconcile
    # retries instead of believing a dead hook is live.
    c = _make_controller()
    c._overlay_tracked_identity = ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )
    c._overlay_window_pid_tid = lambda hwnd: (10, 20)

    # Rejected register -> flag stays False.
    c._overlay_focus_hooks.register_destroy_hook.return_value = False
    c._overlay_destroy_hook_active = False
    c._reconcile_overlay_destroy_hook(True)
    assert c._overlay_destroy_hook_active is False

    # Accepted register -> flag set True.
    c._overlay_focus_hooks.register_destroy_hook.return_value = True
    c._reconcile_overlay_destroy_hook(True)
    assert c._overlay_destroy_hook_active is True


def test_resume_identity_check_matches_and_mismatches(monkeypatch):
    c = _make_controller()
    c._overlay_tracked_identity = ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )

    # Match: current identity equals the tracked one -> valid (restore).
    c._capture_overlay_foreground_identity = lambda: ForegroundIdentity(
        hwnd=777, pid=10, process_name="app.exe", window_creation_time=1
    )
    assert c.overlay_snapshot_is_valid_on_resume() is True

    # HWND-reuse mismatch: same hwnd, different pid -> invalid (re-walk).
    c._capture_overlay_foreground_identity = lambda: ForegroundIdentity(
        hwnd=777, pid=99, process_name="other.exe", window_creation_time=2
    )
    assert c.overlay_snapshot_is_valid_on_resume() is False


def test_resume_identity_check_false_when_no_tracked_identity():
    c = _make_controller()
    c._overlay_tracked_identity = None
    assert c.overlay_snapshot_is_valid_on_resume() is False


# ---------------------------------------------------------------------------
# Menu pop-up open/close re-walk (wh-overlay-menu-close-stale).
#
# Closing a menu does not change the foreground window, so the FOREGROUND hook
# never fires and badges painted for the (now gone) menu items stay floating
# over page controls that have their own badges. The fix registers the
# EVENT_SYSTEM_MENUPOPUPSTART / MENUPOPUPEND range on the same hook thread and
# maps both to the existing FOCUS_CHANGE event: the machine's per-state
# focus-change handling (supersede-refresh when visible, restart while
# building, no-op when closed / paused / error) is exactly the desired menu
# behaviour, so no new event kind or state-machine change is needed.
# ---------------------------------------------------------------------------


def _make_menu_controller(**kwargs):
    """_make_controller plus the menu pop-up callback bound from the class."""
    from main import LogicController

    c = _make_controller(**kwargs)
    c._on_overlay_menu_popup_change = (
        LogicController._on_overlay_menu_popup_change.__get__(c)
    )
    return c


def _drive_to_painted(machine):
    from services.wheelhouse.click_overlay_state import (
        OverlayEvent,
        OverlayEventKind,
        OverlayState,
        PaintAckState,
    )

    machine.apply(OverlayEvent(OverlayEventKind.SHOW_NUMBERS))
    sess, gen = machine.overlay_session_id, machine.paint_generation
    machine.apply(OverlayEvent(OverlayEventKind.BUILD_RESPONSE,
                               overlay_session_id=sess, paint_generation=gen,
                               snapshot_id="snap"))
    machine.apply(OverlayEvent(OverlayEventKind.PAINT_ACK,
                               overlay_session_id=sess, paint_generation=gen,
                               paint_state=PaintAckState.PAINTED))
    assert machine.state is OverlayState.PAINTED


def test_map_menu_popup_event_maps_start_and_end_to_focus_change():
    from services.wheelhouse.click_overlay_state import OverlayEventKind
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
        EVENT_SYSTEM_MENUPOPUPSTART,
        map_menu_popup_event,
    )

    for event_id in (EVENT_SYSTEM_MENUPOPUPSTART, EVENT_SYSTEM_MENUPOPUPEND):
        ev = map_menu_popup_event(event_id=event_id)
        assert ev is not None
        assert ev.kind is OverlayEventKind.FOCUS_CHANGE


def test_map_menu_popup_event_returns_none_for_other_event_ids():
    from services.wheelhouse.overlay_focus_hooks import map_menu_popup_event

    # Foreground (0x0003), MENUSTART (0x0004, the menu-BAR event), and
    # OBJECT_DESTROY (0x8003) must not map -- only the pop-up pair does.
    for event_id in (0x0003, 0x0004, 0x0005, 0x8003, 0):
        assert map_menu_popup_event(event_id=event_id) is None


def test_menu_popup_close_feeds_focus_change_when_painted():
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_menu_controller()
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    # A menu closing over the painted overlay drives the same refresh a focus
    # change does -- the stale menu badges are superseded by a fresh walk.
    assert c.click_overlay_state.state is OverlayState.REFRESH_IN_FLIGHT
    c._perform_overlay_effects.assert_called_once()


def test_menu_popup_open_feeds_focus_change_when_painted():
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPSTART,
    )

    c = _make_menu_controller()
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPSTART)
    # A menu OPENING also re-walks, so the menu items get badges without the
    # user re-saying "show numbers" (the owned-popup walk picks them up).
    assert c.click_overlay_state.state is OverlayState.REFRESH_IN_FLIGHT
    c._perform_overlay_effects.assert_called_once()


def test_menu_popup_shares_foreground_debouncer():
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_menu_controller(debounce_ms=10_000)
    _drive_to_painted(c.click_overlay_state)
    # A foreground change fires first and anchors the SHARED debounce window.
    c._on_overlay_foreground_change(4242)
    assert c.click_overlay_state.state is OverlayState.REFRESH_IN_FLIGHT
    c._perform_overlay_effects.reset_mock()
    # A menu event inside the same window is coalesced: both event sources
    # mean "re-walk against current reality", so one walk serves both.
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    c._perform_overlay_effects.assert_not_called()


def test_menu_popup_gated_on_disabled_config():
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_menu_controller(enabled=False)
    before = c.click_overlay_state.state
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    assert c.click_overlay_state.state is before
    c._perform_overlay_effects.assert_not_called()


def test_menu_popup_unknown_event_id_does_not_advance_debounce():
    # Map-before-debounce ordering: an id the mapper rejects must not burn the
    # debounce window, or the REAL menu-close event arriving right after would
    # be coalesced and the stale badges would survive.
    c = _make_menu_controller()
    c._on_overlay_menu_popup_change(0x0004)  # menu-BAR start: unmapped
    assert c._overlay_focus_debouncer._last_fired_ms is None
    c._perform_overlay_effects.assert_not_called()


def test_menu_popup_closed_state_mints_no_trace_id():
    # Mirrors wh-n29v.22.3 for the menu path: menus open and close all day
    # while the overlay is closed; the NO_OP must not mint trace ids or
    # perform effects.
    from unittest.mock import MagicMock

    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_menu_controller()
    c._mint_overlay_trace_id = MagicMock(return_value="click-deadbeef")
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    c._mint_overlay_trace_id.assert_not_called()
    c._perform_overlay_effects.assert_not_called()


# ---------------------------------------------------------------------------
# Trailing settle re-fire (reviewer_0 finding wh-overlay-nested-dupes.1.1):
# the drop-only debounce loses the FINAL event of a burst (double-Escape,
# open-then-instant-dismiss, dialog-after-menu-close), leaving dead badges.
# A coalesced event now arms a one-shot timer for the debounce-window
# remainder so the final state always gets exactly one re-walk.
# ---------------------------------------------------------------------------


def test_remaining_ms_zero_before_any_fire():
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

    d = FocusChangeDebouncer(debounce_ms=250)
    assert d.remaining_ms(now_ms=1_000.0) == 0.0


def test_remaining_ms_counts_down_from_a_fire_and_clears():
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

    d = FocusChangeDebouncer(debounce_ms=250)
    assert d.should_fire(now_ms=1_000.0)
    assert d.remaining_ms(now_ms=1_000.0) == 250.0
    assert d.remaining_ms(now_ms=1_100.0) == 150.0
    assert d.remaining_ms(now_ms=1_250.0) == 0.0
    assert d.remaining_ms(now_ms=2_000.0) == 0.0


def test_remaining_ms_zero_when_debounce_disabled():
    from services.wheelhouse.overlay_focus_hooks import FocusChangeDebouncer

    d = FocusChangeDebouncer(debounce_ms=0)
    assert d.should_fire(now_ms=1_000.0)
    assert d.remaining_ms(now_ms=1_000.0) == 0.0


def _make_settle_controller(**kwargs):
    """_make_menu_controller plus the settle re-fire seam bound from the class."""
    from unittest.mock import MagicMock

    from main import LogicController

    c = _make_menu_controller(**kwargs)
    for name in (
        "_arm_overlay_settle_refire",
        "_on_overlay_settle_refire",
        "_cancel_overlay_settle_refire",
    ):
        setattr(c, name, getattr(LogicController, name).__get__(c))
    c._overlay_settle_handle = None
    c.loop = MagicMock()
    return c


def test_menu_coalesced_event_arms_settle_timer():
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_settle_controller(debounce_ms=10_000)
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    assert c.click_overlay_state.state is OverlayState.REFRESH_IN_FLIGHT
    c.loop.call_later.assert_not_called()

    # The coalesced second close arms ONE settle timer for the remaining
    # window; the machine is not touched a second time yet.
    c._perform_overlay_effects.reset_mock()
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    c._perform_overlay_effects.assert_not_called()
    assert c.loop.call_later.call_count == 1
    delay_s = c.loop.call_later.call_args[0][0]
    assert 0.0 < delay_s <= 10.0
    assert c._overlay_settle_handle is c.loop.call_later.return_value

    # A third coalesced event does NOT arm a second timer.
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    assert c.loop.call_later.call_count == 1


def test_coalesced_event_while_closed_does_not_arm_settle():
    # GLM finding wh-overlay-nested-dupes.1.5: menus open and close all day
    # while the overlay is CLOSED. A coalesced event used to arm a settle
    # timer even then; if 'show numbers' arrived inside the remaining window,
    # the stale timer fired a FOCUS_CHANGE into the new session's
    # walk_in_flight and superseded the user-requested build (generation bump
    # + an unnecessary second walk). While CLOSED there are no badges to
    # clean up, so a settle serves no purpose -- nothing is armed at all.
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_settle_controller(debounce_ms=10_000)
    assert c.click_overlay_state.state is OverlayState.CLOSED
    # First event anchors the shared debouncer (closed-state record-only).
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    # Second event inside the window is coalesced -- and must NOT arm.
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    c.loop.call_later.assert_not_called()
    assert c._overlay_settle_handle is None


def test_settle_fire_reapplies_focus_change_once_window_clears():
    from services.wheelhouse.click_overlay_state import OverlayState
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_settle_controller(debounce_ms=10_000)
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)   # fires
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)   # coalesced
    c._perform_overlay_effects.reset_mock()

    # Simulate the window clearing (the timer fires at the window edge).
    c._overlay_focus_debouncer.reset()
    c._on_overlay_settle_refire("menu popup closed")

    # The settle fire applies exactly one FOCUS_CHANGE: refresh again so the
    # burst's FINAL state is walked.
    assert c.click_overlay_state.state is OverlayState.REFRESH_IN_FLIGHT
    c._perform_overlay_effects.assert_called_once()
    assert c._overlay_settle_handle is None


def test_settle_fire_rearms_when_window_still_hot():
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_settle_controller(debounce_ms=10_000)
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)   # fires, anchors
    c._perform_overlay_effects.reset_mock()

    # The timer fires while the window is still hot (a real event advanced the
    # anchor after arming): no machine touch; the settle re-arms for the new
    # remainder instead of losing the burst's final state.
    c._on_overlay_settle_refire("menu popup closed")
    c._perform_overlay_effects.assert_not_called()
    assert c.loop.call_later.call_count == 1
    assert c._overlay_settle_handle is c.loop.call_later.return_value


def test_real_fire_cancels_pending_settle():
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_settle_controller(debounce_ms=10_000)
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)   # fires
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)   # coalesced
    handle = c._overlay_settle_handle
    assert handle is not None

    # The window clears and a REAL event fires: the pending settle is
    # cancelled (the real fire already re-walked current reality).
    c._overlay_focus_debouncer.reset()
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)
    handle.cancel.assert_called_once()
    assert c._overlay_settle_handle is None


def test_settle_fire_in_closed_state_is_a_noop():
    from services.wheelhouse.click_overlay_state import OverlayState

    c = _make_settle_controller()
    assert c.click_overlay_state.state is OverlayState.CLOSED
    c._on_overlay_settle_refire("menu popup closed")
    # closed-state FOCUS_CHANGE is a record-only no-op: no effects, no error.
    c._perform_overlay_effects.assert_not_called()
    assert c.click_overlay_state.state is OverlayState.CLOSED


def test_foreground_coalesced_event_also_arms_settle():
    # Scenario (c) of the finding: a menu close fires and anchors the SHARED
    # debouncer; the foreground change of the dialog the menu action opened is
    # coalesced. The settle timer guarantees the dialog still gets one walk.
    from services.wheelhouse.overlay_focus_hooks import (
        EVENT_SYSTEM_MENUPOPUPEND,
    )

    c = _make_settle_controller(debounce_ms=10_000)
    _drive_to_painted(c.click_overlay_state)
    c._on_overlay_menu_popup_change(EVENT_SYSTEM_MENUPOPUPEND)   # fires, anchors
    c._on_overlay_foreground_change(4242)                        # coalesced
    assert c.loop.call_later.call_count == 1
    assert c._overlay_settle_handle is c.loop.call_later.return_value
