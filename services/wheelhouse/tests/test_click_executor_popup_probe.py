"""Popup-closed probe in ClickExecutor pre-click verification (wh-n29v.45).

design-v4.md line 396: pre-click verification adds a "popup HWND still visible
AND still owned by the focused window" check before invoking on a popup-owned
control. Failure -> ``execution_failed:popup_closed`` with the matched name in
the notice. A primary-window control (``source_window_hwnd == 0``) is
unaffected -- the probe does not run and costs no extra COM round-trip.

Driven with the same fakes test_click_executor.py defines, plus injected
``popup_visible_fn`` / ``popup_owner_fn`` seams; headless, no real COM/Win32.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from ui.click_executor import ClickExecutor
from ui.element_types import ElementMatch

from tests.test_click_executor import (
    QUERY,
    FakeControl,
    always_on_screen,
    is_fake_com_error,
    make_match,
    matching_probe,
    probe_fn,
    snap,
)


FOCUSED_HWND = 1000  # == snap().window
POPUP_HWND = 2001


def popup_match(control, *, source_window_hwnd=POPUP_HWND, name="Copy",
                role="menu item"):
    """A popup-owned ElementMatch (carries source_window_hwnd)."""
    base = make_match(control, name=name, role=role)
    return replace(base, source_window_hwnd=source_window_hwnd)


def make_popup_executor(
    *,
    popup_visible_fn,
    popup_owner_fn,
    probe=None,
    on_screen=always_on_screen,
):
    if probe is None:
        probe = matching_probe()  # matches snap(): window=1000
    return ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(probe),
        on_screen_fn=on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
        popup_visible_fn=popup_visible_fn,
        popup_owner_fn=popup_owner_fn,
    )


# ---------------------------------------------------------------------------
# Popup-owned control: probe gates the click.
# ---------------------------------------------------------------------------


def test_popup_owned_passes_when_visible_and_owned():
    control = FakeControl()
    ex = make_popup_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: FOCUSED_HWND,
    )
    result = ex.click(popup_match(control), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1


def test_popup_owned_fails_popup_closed_when_not_visible():
    control = FakeControl()
    ex = make_popup_executor(
        popup_visible_fn=lambda hwnd: False,  # popup window gone
        popup_owner_fn=lambda hwnd: FOCUSED_HWND,
    )
    result = ex.click(popup_match(control, name="Copy"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "popup_closed"
    # The matched name rides the result for the notice wording.
    assert result.matched_name == "Copy"
    # The control was NEVER invoked: the probe fails BEFORE the press.
    assert control.invoke_calls == 0


def test_popup_owned_fails_popup_closed_when_owner_changed():
    control = FakeControl()
    ex = make_popup_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 9999,  # no longer owned by the focused window
    )
    result = ex.click(popup_match(control, name="Paste"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "popup_closed"
    assert result.matched_name == "Paste"
    assert control.invoke_calls == 0


def test_popup_probe_checks_the_correct_popup_hwnd():
    seen_visible: list[int] = []
    seen_owner: list[int] = []

    def visible_fn(hwnd):
        seen_visible.append(hwnd)
        return True

    def owner_fn(hwnd):
        seen_owner.append(hwnd)
        return FOCUSED_HWND

    control = FakeControl()
    ex = make_popup_executor(popup_visible_fn=visible_fn, popup_owner_fn=owner_fn)
    ex.click(popup_match(control, source_window_hwnd=2002), snap(), QUERY)
    # Both seams were asked about the match's OWN popup HWND.
    assert seen_visible == [2002]
    assert seen_owner == [2002]


# ---------------------------------------------------------------------------
# Primary-window control: the probe does NOT run (no extra COM round-trip).
# ---------------------------------------------------------------------------


def test_primary_control_is_unaffected_by_probe():
    """A primary-window match (source_window_hwnd == 0) is clicked without the
    probe ever consulting the popup seams -- no extra COM round-trip."""
    visible_calls: list[int] = []
    owner_calls: list[int] = []
    control = FakeControl()
    ex = make_popup_executor(
        popup_visible_fn=lambda hwnd: visible_calls.append(hwnd) or True,
        popup_owner_fn=lambda hwnd: owner_calls.append(hwnd) or FOCUSED_HWND,
    )
    # make_match() leaves source_window_hwnd at its 0 default.
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"
    # The probe seams were NEVER consulted for a primary-window match.
    assert visible_calls == []
    assert owner_calls == []


def test_default_popup_seams_no_op_for_primary_match():
    """With NO popup seams injected (the Phase 1 construction), a primary-window
    match still clicks normally -- the probe is inert when source_window_hwnd
    is 0."""
    control = FakeControl()
    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
    )
    result = ex.click(make_match(control, name="cancel"), snap(), QUERY)
    assert result.outcome == "ok"


def test_popup_probe_seam_error_fails_closed_popup_closed():
    """A popup seam that RAISES (the popup window raced closed mid-probe) fails
    closed to popup_closed -- never invokes."""
    control = FakeControl()

    def boom(_hwnd):
        raise OSError("popup window gone")

    ex = make_popup_executor(
        popup_visible_fn=boom,
        popup_owner_fn=lambda hwnd: FOCUSED_HWND,
    )
    result = ex.click(popup_match(control, name="Copy"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "popup_closed"
    assert control.invoke_calls == 0


# ---------------------------------------------------------------------------
# Production-wired probe seams (wh-n29v.72.1): the executor runs the REAL
# uia_walker._default_is_window_visible / _default_owner_of probes -- the same
# callables _get_click_executor injects -- faking ONLY win32gui. This composes
# the production wiring instead of injecting bare lambdas, so a regression like
# swapping the two probe kwargs in _get_click_executor (visible<->owner) would
# surface here, not only on a live desktop.
# ---------------------------------------------------------------------------


def make_real_seam_popup_executor():
    """Executor wired with the REAL Win32 probe seams (the production callables).

    Mirrors _get_click_executor's popup_visible_fn / popup_owner_fn injection.
    Only win32gui is faked (per test); the seam functions themselves are the
    production ones.
    """
    from ui import uia_walker

    return ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
        popup_visible_fn=uia_walker._default_is_window_visible,
        popup_owner_fn=uia_walker._default_owner_of,
    )


def test_real_seams_popup_open_clicks_when_visible_and_owned():
    """With the REAL probe seams and win32gui reporting the popup visible AND
    owned by the focused window, _popup_still_open is True -> the popup-owned
    control is clicked. Faking win32gui only (no live desktop)."""
    import win32gui

    control = FakeControl()
    ex = make_real_seam_popup_executor()

    with patch.object(win32gui, "IsWindowVisible", lambda hwnd: True), \
         patch.object(win32gui, "GetWindow", lambda hwnd, _flag: FOCUSED_HWND):
        result = ex.click(popup_match(control, name="Copy"), snap(), QUERY)

    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1


def test_real_seams_popup_closed_when_not_visible():
    """REAL seams + win32gui reporting the popup NOT visible -> _popup_still_open
    is False -> popup_closed, no invoke."""
    import win32gui

    control = FakeControl()
    ex = make_real_seam_popup_executor()

    with patch.object(win32gui, "IsWindowVisible", lambda hwnd: False), \
         patch.object(win32gui, "GetWindow", lambda hwnd, _flag: FOCUSED_HWND):
        result = ex.click(popup_match(control, name="Copy"), snap(), QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "popup_closed"
    assert control.invoke_calls == 0


def test_real_seams_popup_closed_when_owner_changed():
    """REAL seams + win32gui reporting a DIFFERENT owner -> _popup_still_open is
    False -> popup_closed, no invoke. This is the assertion that catches a
    visible<->owner kwarg swap: the owner probe (GetWindow) drives the refusal,
    proving popup_owner_fn is wired to GetWindow(GW_OWNER), not IsWindowVisible."""
    import win32gui

    control = FakeControl()
    ex = make_real_seam_popup_executor()

    with patch.object(win32gui, "IsWindowVisible", lambda hwnd: True), \
         patch.object(win32gui, "GetWindow", lambda hwnd, _flag: 9999):
        result = ex.click(popup_match(control, name="Paste"), snap(), QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "popup_closed"
    assert result.matched_name == "Paste"
    assert control.invoke_calls == 0
