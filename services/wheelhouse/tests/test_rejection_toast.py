"""Tests for the RejectionToast widget action buttons (wh-z7qx1).

Phase 4 of wh-9weum adds two buttons to the advisory toast:

  * "Try it anyway" -- visible only on uncertain rejects
    (default_reject_paste_capable_class). Hidden on denylist hits,
    the wh-zndq browser trap, and transient/no-effect cases
    (stale_com, not_focusable). Emits a Qt ``try_anyway_clicked``
    signal carrying nothing -- the GUI manager attaches the
    correlation token and forwards via IPC under wh-iycks.

  * "Show details" -- always shown. One-way expansion: clicking
    once shows the details panel and removes the button. There is
    no Hide-details affordance.

Full Qt-test coverage of every signal path lives in wh-6q4x; this
file covers only the visibility rules and the show-details
one-way expansion that wh-z7qx1 introduces.
"""

from __future__ import annotations

import pytest

from rejection_toast import RejectionToast
from rejection_toast_wording import (
    CATEGORY_BROWSER_TRAP,
    CATEGORY_DEFINITELY_NOT_TEXT,
    CATEGORY_OTHER,
    CATEGORY_UNCERTAIN,
    ToastWording,
    compose_rejection_wording,
    should_show_try_anyway,
)
from shared.text_target_rejection import (
    REASON_DEFAULT_REJECT,
    REASON_DEFAULT_REJECT_PASTE_CAPABLE_CLASS,
    REASON_DENYLIST_CLASS_NAME,
    REASON_DENYLIST_CONTROL_TYPE,
    REASON_NOT_FOCUSABLE,
    REASON_STALE_COM,
)


def _wording(category: str = CATEGORY_UNCERTAIN) -> ToastWording:
    return ToastWording(
        title="Wheelhouse couldn't type that",
        body="body",
        category=category,
    )


class TestShouldShowTryAnyway:
    """Visibility classifier (single source of truth for the rule)."""

    def test_uncertain_shows(self):
        assert should_show_try_anyway(CATEGORY_UNCERTAIN) is True

    def test_browser_trap_hides(self):
        assert should_show_try_anyway(CATEGORY_BROWSER_TRAP) is False

    def test_definitely_not_text_hides(self):
        assert should_show_try_anyway(CATEGORY_DEFINITELY_NOT_TEXT) is False

    def test_other_hides(self):
        # stale_com, not_focusable, and any future unknown reason
        # land here. Must hide, never override.
        assert should_show_try_anyway(CATEGORY_OTHER) is False

    @pytest.mark.parametrize(
        "reason,expected_category",
        [
            (REASON_DEFAULT_REJECT_PASTE_CAPABLE_CLASS, CATEGORY_UNCERTAIN),
            (REASON_DENYLIST_CLASS_NAME, CATEGORY_DEFINITELY_NOT_TEXT),
            (REASON_DENYLIST_CONTROL_TYPE, CATEGORY_DEFINITELY_NOT_TEXT),
            (REASON_STALE_COM, CATEGORY_OTHER),
            (REASON_NOT_FOCUSABLE, CATEGORY_OTHER),
        ],
    )
    def test_reason_constants_route_through_wording_helper(
        self, reason: str, expected_category: str,
    ):
        # Anchors the constants to the wording helper's classification
        # so a future rename in either place fails this test, not at
        # runtime in the GUI process.
        wording = compose_rejection_wording(
            reason=reason,
            control_type="ButtonControl"
            if reason == REASON_DENYLIST_CONTROL_TYPE
            else "WindowControl",
            process_name="zed.exe",
            class_name="MenuFlyoutSubItem"
            if reason == REASON_DENYLIST_CLASS_NAME
            else "Zed::Window",
        )
        assert wording.category == expected_category

    def test_browser_trap_via_helper(self):
        wording = compose_rejection_wording(
            reason=REASON_DEFAULT_REJECT,
            control_type="DocumentControl",
            process_name="brave.exe",
            class_name="",
        )
        assert wording.category == CATEGORY_BROWSER_TRAP
        assert should_show_try_anyway(wording.category) is False


class TestTryAnywayButtonVisibility:
    def test_try_anyway_visible_on_uncertain(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(CATEGORY_UNCERTAIN),
                details=["irrelevant"],
            )
            assert toast._try_anyway_button.isVisible() is True
        finally:
            toast.close()
            toast.deleteLater()

    @pytest.mark.parametrize(
        "category",
        [
            CATEGORY_BROWSER_TRAP,
            CATEGORY_DEFINITELY_NOT_TEXT,
            CATEGORY_OTHER,
        ],
    )
    def test_try_anyway_hidden_on_other_categories(
        self, qapp, category: str,
    ):
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(category),
                details=["irrelevant"],
            )
            assert toast._try_anyway_button.isVisible() is False
        finally:
            toast.close()
            toast.deleteLater()

    def test_try_anyway_visibility_resets_between_shows(self, qapp):
        # A toast instance is reused; a denylist toast followed by an
        # uncertain toast must show the button on the second render
        # even though the first hid it.
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(CATEGORY_DEFINITELY_NOT_TEXT),
                details=[],
            )
            assert toast._try_anyway_button.isVisible() is False
            toast.show_rejection(
                _wording(CATEGORY_UNCERTAIN),
                details=[],
            )
            assert toast._try_anyway_button.isVisible() is True
        finally:
            toast.close()
            toast.deleteLater()


class TestTryAnywayClickEmitsSignal:
    def test_click_emits_try_anyway_clicked(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(CATEGORY_UNCERTAIN),
                details=[],
            )
            received: list[bool] = []
            toast.try_anyway_clicked.connect(lambda: received.append(True))
            toast._try_anyway_button.click()
            assert received == [True]
        finally:
            toast.close()
            toast.deleteLater()

    def test_double_click_emits_signal_once(self, qapp):
        # wh-vbvgf.2.2: a fast second click must NOT re-fire the signal.
        # Without the debounce, two clicks would each run the full retry
        # pipeline (cache lookup, ClipboardOnlyStrategy paste, clipboard
        # save/restore) and the user would see the dictation pasted twice.
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            received: list[bool] = []
            toast.try_anyway_clicked.connect(lambda: received.append(True))

            toast._try_anyway_button.click()
            toast._try_anyway_button.click()

            assert received == [True]
            assert toast._try_anyway_button.isEnabled() is False
        finally:
            toast.close()
            toast.deleteLater()

    def test_button_re_enabled_on_next_show(self, qapp):
        # The next rejection toast must show a fresh, ready button. The
        # widget is reused across rejection events, so a previous show
        # that disabled the button must not leak into the next render.
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            toast._try_anyway_button.click()
            assert toast._try_anyway_button.isEnabled() is False

            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            assert toast._try_anyway_button.isEnabled() is True
        finally:
            toast.close()
            toast.deleteLater()


class TestAutoDismissTimer:
    """wh-6q4x coverage: the lifetime timer fires close() at the
    configured ms. After the timer fires the toast is hidden, and a
    re-show resets the dwell so the next presentation gets a fresh
    timer."""

    def test_lifetime_timer_closes_widget(self, qtbot):
        toast = RejectionToast()
        qtbot.addWidget(toast)
        try:
            toast.show_rejection(_wording(), details=[], lifetime_ms=500)
            assert toast.isVisible()
            qtbot.waitUntil(lambda: not toast.isVisible(), timeout=2000)
        finally:
            toast.deleteLater()

    def test_re_show_resets_lifetime(self, qtbot):
        toast = RejectionToast()
        qtbot.addWidget(toast)
        try:
            toast.show_rejection(_wording(), details=[], lifetime_ms=500)
            # Re-show with a longer lifetime before the original
            # timer would fire.
            toast.show_rejection(_wording(), details=[], lifetime_ms=2000)
            # After 800ms the toast is still visible because the
            # second show set a fresh 2000ms lifetime.
            qtbot.wait(800)
            assert toast.isVisible()
        finally:
            toast.deleteLater()

    def test_lifetime_clamped_to_minimum(self, qtbot):
        """The widget clamps lifetime_ms to a 500ms minimum to prevent
        an instant-close on a misconfigured caller.

        wh-vbvgf.14.2: the test asserts the timer's interval directly
        instead of inferring the floor from wall-clock visibility.
        Direct assertion catches any clamp regression at all (not
        just <400ms) and removes timing flakiness from CI scheduling.
        wh-vbvgf.15.2 (deepseek): added the direct interval check
        alongside the existing wall-clock waits, which still verify
        the timer signal is wired and actually fires."""

        toast = RejectionToast()
        qtbot.addWidget(toast)
        try:
            toast.show_rejection(_wording(), details=[], lifetime_ms=10)
            # Direct floor verification: the timer interval must be at
            # least 500ms regardless of the input lifetime_ms.
            assert toast._lifetime_timer.interval() >= 500, (
                "lifetime clamp regressed below 500ms -- "
                f"timer interval is {toast._lifetime_timer.interval()}ms"
            )
            # Wall-clock check still runs alongside to verify the
            # timer signal is wired and fires.
            qtbot.wait(400)
            assert toast.isVisible()
            qtbot.waitUntil(lambda: not toast.isVisible(), timeout=2000)
        finally:
            toast.deleteLater()


class TestXDismiss:
    """wh-vbvgf.14.3 (codex review): the rejection toast X button must
    hide the widget when clicked, and must not carry expanded details
    or button state into the next show."""

    def test_x_click_hides_widget(self, qtbot):
        toast = RejectionToast()
        qtbot.addWidget(toast)
        try:
            toast.show_rejection(_wording(), details=["a"])
            assert toast.isVisible()
            toast._dismiss_button.click()
            qtbot.waitUntil(lambda: not toast.isVisible(), timeout=1000)
        finally:
            toast.deleteLater()

    def test_x_click_after_show_details_resets_details_on_reshow(self, qtbot):
        """A user expands details, dismisses with X, then sees a new
        rejection. The new toast must not carry the expanded details
        panel or the missing details button from the previous show."""

        toast = RejectionToast()
        qtbot.addWidget(toast)
        try:
            toast.show_rejection(_wording(), details=["fields"])
            toast._details_button.click()
            assert toast._details_label.isVisible()
            assert not toast._details_button.isVisible()

            toast._dismiss_button.click()
            qtbot.waitUntil(lambda: not toast.isVisible(), timeout=1000)

            # New rejection: details panel should be hidden again,
            # details button should be visible.
            toast.show_rejection(_wording(), details=["new"])
            assert toast._details_label.isVisible() is False
            assert toast._details_button.isVisible() is True
        finally:
            toast.deleteLater()


class TestShowDetailsOneWayExpansion:
    def test_details_button_visible_initially(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(), details=["a", "b"])
            assert toast._details_button.isVisible() is True
            assert toast._details_label.isVisible() is False
        finally:
            toast.close()
            toast.deleteLater()

    def test_click_reveals_details_and_hides_button(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(), details=["line1", "line2"])
            toast._details_button.click()
            assert toast._details_label.isVisible() is True
            assert "line1" in toast._details_label.text()
            assert "line2" in toast._details_label.text()
            # One-way: button does not stay around as Hide-details.
            assert toast._details_button.isVisible() is False
        finally:
            toast.close()
            toast.deleteLater()

    def test_details_button_resets_visible_between_shows(self, qapp):
        # After a toast is dismissed, the next show must restore the
        # collapsed-with-button initial state. Otherwise a user who
        # clicked Show details once would never see the button again.
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(), details=["x"])
            toast._details_button.click()
            assert toast._details_button.isVisible() is False
            toast.show_rejection(_wording(), details=["y"])
            assert toast._details_button.isVisible() is True
            assert toast._details_label.isVisible() is False
        finally:
            toast.close()
            toast.deleteLater()


class TestWhyButtonVisibility:
    """wh-b9kpc: Why-am-I-seeing-this button visibility follows the
    same rule as Try-it-anyway. The button is only useful when the
    user has a Try-it-anyway path; for the other categories the
    notice is silenced entirely after wh-1r2b3, so the visibility
    rule is trivially satisfied in production. The tests anchor the
    rule directly anyway so a future change to the category logic
    cannot quietly leak a Why button into a silenced notice."""

    def test_why_visible_on_uncertain(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(CATEGORY_UNCERTAIN),
                details=["irrelevant"],
            )
            assert toast._why_button.isVisible() is True
        finally:
            toast.close()
            toast.deleteLater()

    @pytest.mark.parametrize(
        "category",
        [
            CATEGORY_BROWSER_TRAP,
            CATEGORY_DEFINITELY_NOT_TEXT,
            CATEGORY_OTHER,
        ],
    )
    def test_why_hidden_on_other_categories(
        self, qapp, category: str,
    ):
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(category),
                details=["irrelevant"],
            )
            assert toast._why_button.isVisible() is False
        finally:
            toast.close()
            toast.deleteLater()

    def test_why_visibility_resets_between_shows(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(CATEGORY_DEFINITELY_NOT_TEXT),
                details=[],
            )
            assert toast._why_button.isVisible() is False
            toast.show_rejection(
                _wording(CATEGORY_UNCERTAIN),
                details=[],
            )
            assert toast._why_button.isVisible() is True
        finally:
            toast.close()
            toast.deleteLater()

    def test_why_visibility_resets_visible_to_hidden(self, qapp):
        # wh-b9kpc.2.1 (deepseek review): the reset direction
        # UNCERTAIN -> DEFINITELY_NOT_TEXT is the asymmetric case
        # the existing reset test missed. The forward direction
        # (hidden -> visible) passes even if show_rejection forgets
        # to call setVisible on the why button, because the button
        # defaults to hidden in _build_ui. Without this test, a
        # regression that drops the setVisible(False) reset on a
        # silenced category would leak a stale visible button into
        # the next show_rejection call.
        toast = RejectionToast()
        try:
            toast.show_rejection(
                _wording(CATEGORY_UNCERTAIN),
                details=[],
            )
            assert toast._why_button.isVisible() is True
            toast.show_rejection(
                _wording(CATEGORY_DEFINITELY_NOT_TEXT),
                details=[],
            )
            assert toast._why_button.isVisible() is False
        finally:
            toast.close()
            toast.deleteLater()

    def test_why_button_label(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            assert toast._why_button.text() == "Why am I seeing this?"
        finally:
            toast.close()
            toast.deleteLater()


class TestWhyButtonClick:
    """wh-b9kpc: clicking the Why button opens the help window. The
    window stays open while the user reads and does not dismiss the
    rejection notice. Subsequent clicks bring the same window forward
    instead of spawning a new one."""

    def test_click_opens_help_window(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            assert toast._help_window is None
            toast._why_button.click()
            assert toast._help_window is not None
            assert toast._help_window.isVisible() is True
        finally:
            if toast._help_window is not None:
                toast._help_window.close()
                toast._help_window.deleteLater()
            toast.close()
            toast.deleteLater()

    def test_click_does_not_dismiss_toast(self, qapp):
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            assert toast.isVisible() is True
            toast._why_button.click()
            assert toast.isVisible() is True
        finally:
            if toast._help_window is not None:
                toast._help_window.close()
                toast._help_window.deleteLater()
            toast.close()
            toast.deleteLater()

    def test_second_click_reuses_window_and_re_shows(self, qapp):
        # wh-b9kpc local review finding 4: assert identity AND that
        # the second click actually re-shows the window. A regression
        # where _on_why_clicked stops calling show() on the reused
        # instance would leave the user staring at no response.
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            toast._why_button.click()
            first_window = toast._help_window
            assert first_window is not None
            assert first_window.isVisible() is True

            first_window.close()
            assert first_window.isVisible() is False

            toast._why_button.click()
            assert toast._help_window is first_window
            assert first_window.isVisible() is True
        finally:
            if toast._help_window is not None:
                toast._help_window.close()
                toast._help_window.deleteLater()
            toast.close()
            toast.deleteLater()

    def test_closing_help_window_leaves_toast_visible(self, qapp):
        # wh-b9kpc local review finding 5: assert the help window is
        # actually hidden after close, not just that the toast is
        # still visible. A regression where close() becomes a no-op
        # (or a closeEvent re-shows) would slip through without the
        # explicit hidden assertion.
        toast = RejectionToast()
        try:
            toast.show_rejection(_wording(CATEGORY_UNCERTAIN), details=[])
            toast._why_button.click()
            help_window = toast._help_window
            assert help_window is not None
            help_window.close()
            assert help_window.isVisible() is False
            assert toast.isVisible() is True
        finally:
            if toast._help_window is not None:
                toast._help_window.deleteLater()
            toast.close()
            toast.deleteLater()


class TestClampFrameToAvailable:
    """wh-b9kpc.1.1 (codex review): the clamping logic must
    constrain the window's full frame to the available screen
    geometry, not just its center. The earlier version checked
    screenAt(center) only, which let a frame whose center was
    on-screen but whose bottom edge was off-screen pass without
    being moved (codex reproduced this on a DPR-3.0 1232x720
    logical screen where Qt does not pre-clamp).

    The pure clamping function is tested independent of the OS
    window manager, which on Windows auto-clamps via the platform
    plugin and would hide the bug this function protects against.
    """

    def test_fully_inside_returns_same_position(self):
        # Frame at (50, 50) size 500x624 inside (0, 0, 1232, 720).
        x, y = RejectionToast._clamp_frame_to_available(
            50, 50, 500, 624,
            0, 0, 1232, 720,
        )
        assert (x, y) == (50, 50)

    def test_right_edge_extends_clamps_left(self):
        # Frame right edge would be at 1300, exceeds available 1232.
        # Clamp so right edge equals 1232: new_x = 1232 - 500 = 732.
        x, y = RejectionToast._clamp_frame_to_available(
            800, 50, 500, 624,
            0, 0, 1232, 720,
        )
        assert (x, y) == (732, 50)

    def test_bottom_edge_extends_clamps_up(self):
        # The exact case codex reproduced: frame at (722, 200) size
        # 500x624 with available 1232x720. Bottom edge at 824
        # exceeds 720. Clamp so bottom equals 720: new_y = 720 - 624
        # = 96.
        x, y = RejectionToast._clamp_frame_to_available(
            722, 200, 500, 624,
            0, 0, 1232, 720,
        )
        assert (x, y) == (722, 96)

    def test_left_edge_negative_clamps_right(self):
        # Frame x = -50 on an available starting at 0.
        x, y = RejectionToast._clamp_frame_to_available(
            -50, 100, 500, 624,
            0, 0, 1232, 720,
        )
        assert (x, y) == (0, 96)

    def test_top_edge_negative_clamps_down(self):
        x, y = RejectionToast._clamp_frame_to_available(
            100, -50, 500, 624,
            0, 0, 1232, 720,
        )
        assert (x, y) == (100, 0)

    def test_frame_larger_than_available_pins_to_top_left(self):
        # Frame 800x900, available 600x500. Both dimensions exceed.
        # Pin to (avail_x, avail_y); the scroll area inside the body
        # handles the overflow so Close stays reachable.
        x, y = RejectionToast._clamp_frame_to_available(
            300, 400, 800, 900,
            0, 0, 600, 500,
        )
        assert (x, y) == (0, 0)

    def test_available_with_nonzero_origin(self):
        # Multi-monitor: secondary screen offset at (1920, 0).
        # Frame would be off the right edge of the secondary screen
        # but fits vertically.
        x, y = RejectionToast._clamp_frame_to_available(
            3500, 50, 500, 624,
            1920, 0, 1280, 720,
        )
        # Right edge clamp: new_x = 1920 + (1280-500) = 2700.
        # Vertical: 50 + 624 = 674 <= 720, no clamp needed.
        assert (x, y) == (2700, 50)


class TestEnsureHelpWindowOnScreenSmoke:
    """Smoke test that the wrapper composes the pure clamp function
    with the real window's geometry without raising. The clamping
    correctness is exercised by TestClampFrameToAvailable."""

    def test_runs_without_exception_on_real_window(self, qapp):
        from rejection_help_window import RejectionHelpWindow

        win = RejectionHelpWindow()
        try:
            win.show()
            RejectionToast._ensure_help_window_on_screen(win)
        finally:
            win.close()
            win.deleteLater()

    def test_delta_correction_moves_inner_geometry(self, qapp, monkeypatch):
        # wh-b9kpc.2.2 (deepseek review): the wrapper's frame-to-inner
        # delta correction is the part the pure-function tests do not
        # cover. Force the clamp helper to return a position different
        # from the current frame, then assert window.move was called
        # with the inner top-left offset by the same delta as the
        # frame's requested shift.
        from rejection_help_window import RejectionHelpWindow
        import rejection_toast as rt_module

        win = RejectionHelpWindow()
        moves: list[tuple[int, int]] = []
        original_move = win.move

        def record_move(x, y):
            moves.append((int(x), int(y)))
            original_move(x, y)

        try:
            win.show()
            # Force a clamp that shifts the frame by (-10, -20).
            frame_before = win.frameGeometry()
            inner_before = win.geometry()
            target_frame_x = frame_before.x() - 10
            target_frame_y = frame_before.y() - 20

            def fake_clamp(fx, fy, fw, fh, ax, ay, aw, ah):
                return target_frame_x, target_frame_y

            monkeypatch.setattr(
                rt_module.RejectionToast,
                "_clamp_frame_to_available",
                staticmethod(fake_clamp),
            )
            monkeypatch.setattr(win, "move", record_move)

            RejectionToast._ensure_help_window_on_screen(win)

            # The wrapper must shift the inner by the same delta
            # that the clamp shifted the frame. If the delta is
            # wrong (e.g. dropped, or applied as an absolute frame
            # coordinate), the move arguments will not match.
            assert len(moves) == 1
            expected_inner_x = inner_before.x() + (target_frame_x - frame_before.x())
            expected_inner_y = inner_before.y() + (target_frame_y - frame_before.y())
            assert moves[0] == (expected_inner_x, expected_inner_y)
        finally:
            win.close()
            win.deleteLater()

    def test_no_clamp_means_no_move(self, qapp, monkeypatch):
        # wh-b9kpc.2.2 supplement: when the clamp returns the same
        # position the frame already has, the wrapper must NOT call
        # window.move at all. This protects against a future change
        # that drops the early return guard and starts firing
        # spurious move() calls every Why click.
        from rejection_help_window import RejectionHelpWindow
        import rejection_toast as rt_module

        win = RejectionHelpWindow()
        moves: list[tuple[int, int]] = []
        original_move = win.move

        def record_move(x, y):
            moves.append((int(x), int(y)))
            original_move(x, y)

        try:
            win.show()
            frame = win.frameGeometry()

            def identity_clamp(fx, fy, fw, fh, ax, ay, aw, ah):
                return fx, fy

            monkeypatch.setattr(
                rt_module.RejectionToast,
                "_clamp_frame_to_available",
                staticmethod(identity_clamp),
            )
            monkeypatch.setattr(win, "move", record_move)

            RejectionToast._ensure_help_window_on_screen(win)

            assert moves == []
        finally:
            win.close()
            win.deleteLater()
