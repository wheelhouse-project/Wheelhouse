"""Shell-window probe in ClickExecutor pre-click verification
(wh-overlay-taskbar-numbers.3).

A taskbar match (``source_window_is_shell=True``) comes from an UNOWNED
always-on-top shell window (``Shell_TrayWnd`` / ``Shell_SecondaryTrayWnd`` /
a tray-overflow window), so the popup probe's owner check
(``popup_owner_fn(hwnd) == focused_hwnd``) would refuse every taskbar click
as ``popup_closed``. The shell branch requires that the shell window is
still visible AND that its class name is still one of the taskbar shell
classes -- HWNDs are recycled, so after an explorer restart the walked
handle can name an unrelated visible window that visibility alone would
pass (codex finding wh-overlay-taskbar-numbers.5.3). Failure ->
``execution_failed:taskbar_closed`` with the matched name in the notice.
The owner seam is NEVER consulted for a shell match. Popup-owned matches
(``source_window_is_shell=False``) keep the full visible-AND-owned probe.

Driven with the same fakes test_click_executor.py defines; headless, no real
COM/Win32 (the real-seam tests fake win32gui only, mirroring
test_click_executor_popup_probe.py).
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch

from ui.click_executor import ClickExecutor

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
TASKBAR_HWND = 3001


def shell_match(control, *, source_window_hwnd=TASKBAR_HWND, name="Start",
                role="button"):
    """A taskbar-shell ElementMatch (shell HWND + the shell marker)."""
    base = make_match(control, name=name, role=role)
    return replace(
        base,
        source_window_hwnd=source_window_hwnd,
        source_window_is_shell=True,
    )


def make_shell_executor(
    *,
    popup_visible_fn,
    popup_owner_fn,
    shell_class_fn=lambda hwnd: "Shell_TrayWnd",
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
        shell_class_fn=shell_class_fn,
    )


# ---------------------------------------------------------------------------
# Shell match: visibility alone gates the click; the owner seam never runs.
# ---------------------------------------------------------------------------


def test_shell_match_passes_when_shell_window_visible():
    """The real taskbar is UNOWNED (owner 0 != focused 1000): the popup
    probe would refuse it, the shell branch clicks it. The owner seam is
    never consulted."""
    owner_calls: list[int] = []
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: owner_calls.append(hwnd) or 0,
    )
    result = ex.click(shell_match(control), snap(), QUERY)
    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1
    assert owner_calls == []


def test_shell_match_fails_taskbar_closed_when_not_visible():
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: False,  # taskbar/overflow window gone
        popup_owner_fn=lambda hwnd: 0,
    )
    result = ex.click(shell_match(control, name="Start"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    # The matched name rides the result for the notice wording.
    assert result.matched_name == "Start"
    # The control was NEVER invoked: the probe fails BEFORE the press.
    assert control.invoke_calls == 0


def test_shell_probe_checks_the_correct_shell_hwnd():
    seen_visible: list[int] = []
    seen_owner: list[int] = []

    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: seen_visible.append(hwnd) or True,
        popup_owner_fn=lambda hwnd: seen_owner.append(hwnd) or 0,
    )
    ex.click(shell_match(control, source_window_hwnd=3002), snap(), QUERY)
    # The visible seam was asked about the match's OWN shell HWND; the
    # owner seam was never consulted (shell windows are unowned).
    assert seen_visible == [3002]
    assert seen_owner == []


def test_shell_probe_seam_error_fails_closed_taskbar_closed():
    """A visible seam that RAISES (explorer restarting mid-probe) fails
    closed to taskbar_closed -- never invokes."""
    control = FakeControl()

    def boom(_hwnd):
        raise OSError("shell window gone")

    ex = make_shell_executor(
        popup_visible_fn=boom,
        popup_owner_fn=lambda hwnd: 0,
    )
    result = ex.click(shell_match(control, name="Start"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert control.invoke_calls == 0


def test_shell_probe_missing_visible_seam_fails_closed():
    """With NO probe seams injected (a construction that never wired them),
    a shell match fails CLOSED to taskbar_closed -- same discipline as the
    popup probe's None-seam refusal, under the shell tag."""
    control = FakeControl()
    ex = ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
    )
    result = ex.click(shell_match(control, name="Start"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert control.invoke_calls == 0


# ---------------------------------------------------------------------------
# HWND-reuse identity check (codex finding wh-overlay-taskbar-numbers.5.3):
# visibility alone can pass against a RECYCLED handle naming an unrelated
# window, so the probe also requires the current class to still be one of
# the taskbar shell classes. Same fail-closed discipline as the other seams.
# ---------------------------------------------------------------------------


def test_shell_match_refused_when_class_no_longer_shell():
    """Explorer restarted and Windows recycled the walked HWND for an
    unrelated VISIBLE window: the class re-read refuses the click."""
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 0,
        shell_class_fn=lambda hwnd: "Notepad",  # reused, not a shell window
    )
    result = ex.click(shell_match(control, name="Start"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert control.invoke_calls == 0


def test_shell_match_accepts_any_taskbar_shell_class():
    # The check is set membership over TASKBAR_WINDOW_CLASSES, not equality
    # with Shell_TrayWnd -- an overflow-flyout badge stays clickable.
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 0,
        shell_class_fn=lambda hwnd: "NotifyIconOverflowWindow",
    )
    result = ex.click(shell_match(control), snap(), QUERY)
    assert result.outcome == "ok"
    assert control.invoke_calls == 1


def test_shell_probe_class_checked_for_the_matched_hwnd():
    seen_class: list[int] = []
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 0,
        shell_class_fn=lambda hwnd: seen_class.append(hwnd)
        or "Shell_TrayWnd",
    )
    ex.click(shell_match(control, source_window_hwnd=3002), snap(), QUERY)
    assert seen_class == [3002]


def test_shell_probe_missing_class_seam_fails_closed():
    """A construction that wired the visibility seam but not the class seam
    cannot confirm the HWND still names a shell window -> fail closed."""
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 0,
        shell_class_fn=None,
    )
    result = ex.click(shell_match(control, name="Start"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert control.invoke_calls == 0


def test_shell_probe_class_seam_error_fails_closed():
    control = FakeControl()

    def boom(_hwnd):
        raise OSError("window gone mid-probe")

    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 0,
        shell_class_fn=boom,
    )
    result = ex.click(shell_match(control, name="Start"), snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert control.invoke_calls == 0


# ---------------------------------------------------------------------------
# The popup branch is unchanged: an owned popup still requires visible AND
# owned. Guards the branch ordering in _verify.
# ---------------------------------------------------------------------------


def test_popup_match_still_requires_owner():
    control = FakeControl()
    ex = make_shell_executor(
        popup_visible_fn=lambda hwnd: True,
        popup_owner_fn=lambda hwnd: 9999,  # not the focused window
    )
    popup = replace(
        make_match(control, name="Copy", role="menu item"),
        source_window_hwnd=2001,
    )
    result = ex.click(popup, snap(), QUERY)
    assert result.outcome == "execution_failed"
    assert result.reason == "popup_closed"
    assert control.invoke_calls == 0


# ---------------------------------------------------------------------------
# Production-wired probe seams: the executor runs the REAL
# uia_walker._default_is_window_visible probe -- the same callable
# _get_click_executor injects -- faking ONLY win32gui. GetWindow(GW_OWNER)
# returning 0 is exactly what the real unowned taskbar reports, so these
# tests prove the production wiring clicks the taskbar where the popup
# probe would have refused it.
# ---------------------------------------------------------------------------


def make_real_seam_shell_executor():
    from ui import uia_walker

    return ClickExecutor(
        coordinate_click_fn=lambda _x, _y: (True, 2),
        foreground_probe=probe_fn(matching_probe()),
        on_screen_fn=always_on_screen,
        com_error_predicate=is_fake_com_error,
        invoke_fn=lambda ref: ref.Invoke(),
        popup_visible_fn=uia_walker._default_is_window_visible,
        popup_owner_fn=uia_walker._default_owner_of,
        shell_class_fn=uia_walker._default_class_name_of,
    )


def test_real_seams_shell_clicks_when_visible_even_though_unowned():
    import win32gui

    control = FakeControl()
    ex = make_real_seam_shell_executor()

    with patch.object(win32gui, "IsWindowVisible", lambda hwnd: True), \
         patch.object(win32gui, "GetWindow", lambda hwnd, _flag: 0), \
         patch.object(win32gui, "GetClassName",
                      lambda hwnd: "Shell_TrayWnd"):
        result = ex.click(shell_match(control, name="Start"), snap(), QUERY)

    assert result.outcome == "ok"
    assert result.clicked_via == "invoke"
    assert control.invoke_calls == 1


def test_real_seams_taskbar_closed_when_not_visible():
    import win32gui

    control = FakeControl()
    ex = make_real_seam_shell_executor()

    with patch.object(win32gui, "IsWindowVisible", lambda hwnd: False), \
         patch.object(win32gui, "GetWindow", lambda hwnd, _flag: 0), \
         patch.object(win32gui, "GetClassName",
                      lambda hwnd: "Shell_TrayWnd"):
        result = ex.click(shell_match(control, name="Start"), snap(), QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert result.matched_name == "Start"
    assert control.invoke_calls == 0


def test_real_seams_taskbar_closed_when_hwnd_reused_by_non_shell_window():
    """Production wiring, reuse case: IsWindowVisible passes (the recycled
    HWND names a live window) but GetClassName no longer reports a shell
    class -> refused, never invoked."""
    import win32gui

    control = FakeControl()
    ex = make_real_seam_shell_executor()

    with patch.object(win32gui, "IsWindowVisible", lambda hwnd: True), \
         patch.object(win32gui, "GetWindow", lambda hwnd, _flag: 0), \
         patch.object(win32gui, "GetClassName",
                      lambda hwnd: "SunAwtFrame"):
        result = ex.click(shell_match(control, name="Start"), snap(), QUERY)

    assert result.outcome == "execution_failed"
    assert result.reason == "taskbar_closed"
    assert control.invoke_calls == 0
