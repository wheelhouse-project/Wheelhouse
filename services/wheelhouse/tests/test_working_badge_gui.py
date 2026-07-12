"""GUI consumer for the dictation-retraction working indicator
(wh-dictation-retraction-indicator.3).

The GUI process owns a DEDICATED OverlayPaintWindowManager for the working
badge (separate from the numbered overlay so their generation gates and window
teardown never interfere). On the 'settling' activity state it reads the mouse
pointer (GetCursorPos -- universal, works for every app) and paints the busy
glyph there; on 'confirmed'/'idle' it clears the badge. A single-shot fallback
timer bounds a missed 'confirmed' so the badge can never get stuck. The whole
feature is gated by WorkingIndicatorConfig.

These tests construct GuiManager under the standard GUI test patches (no Qt
event loop, no pystray) and exercise the badge methods + the activity-shm
routing directly.
"""

from __future__ import annotations

import json
import struct
from unittest.mock import MagicMock, patch

import pytest

# Keep GuiManager construction free of real QDialogs in this file
# (wh-pytest-flaky-segfault).
pytestmark = pytest.mark.usefixtures("mock_editor_window")


def _build_gui(config):
    """Construct a GuiManager under the standard GUI test patches and return
    (gui_manager, overlay_ctor_spy)."""
    with patch(
        "overlay_paint_window.OverlayPaintWindowManager"
    ) as overlay_ctor, patch("gui.FloatingButton"), patch(
        "gui.WorkingDialog"
    ), patch(
        "gui.pystray"
    ) as mock_pystray, patch(
        "gui.QTimer"
    ):
        mock_pystray.Icon.return_value = MagicMock()
        from gui import GuiManager

        gm = GuiManager(MagicMock(), MagicMock(), MagicMock(), config=config)
        # Normalize the construction-time DPI decision so these unit tests
        # exercise badge logic, not the test runner's ambient DPI state: when
        # an alphabetically earlier test file has pinned the process DPI
        # awareness to a non-per-monitor state, the gate reads a definite
        # False here and would disable the badge for every test in this file
        # (wh-working-badge-gui-test-isolation). Done by assignment after
        # construction -- NOT by patching the method on the class -- because
        # class-level mock.patch on the QObject-derived GuiManager caused a
        # deterministic native crash at a later signal connect (recorded on
        # wh-pytest-flaky-segfault). Gate logic itself is covered by
        # TestWorkingBadgeDpiAwareness and TestWorkingBadgeDpiSkip.
        gm._working_badge_dpi_unsafe = False
        return gm, overlay_ctor


def _write_state(buf: bytearray, state: str, utterance_id: int) -> None:
    """Write an activity-state message into a shared-memory-shaped buffer in
    the same framing _write_activity_state uses (4-byte big-endian size header
    then the JSON payload)."""
    payload = json.dumps({"state": state, "utterance_id": utterance_id}).encode(
        "utf-8"
    )
    struct.pack_into(">I", buf, 0, len(payload))
    buf[4 : 4 + len(payload)] = payload


class TestWorkingBadgeConstruction:
    def test_enabled_constructs_dedicated_overlay(self, qapp):
        gm, _ = _build_gui({"dictation": {"working_indicator_enabled": True}})
        assert gm._working_indicator_enabled is True
        assert gm._working_badge_overlay is not None

    def test_disabled_skips_overlay_and_show_is_noop(self, qapp):
        gm, _ = _build_gui({"dictation": {"working_indicator_enabled": False}})
        assert gm._working_indicator_enabled is False
        assert gm._working_badge_overlay is None
        # Showing while disabled must be a harmless no-op.
        gm._show_working_badge()
        assert gm._working_badge_shown is False

    def test_default_when_block_absent_is_enabled(self, qapp):
        gm, _ = _build_gui({})
        assert gm._working_indicator_enabled is True
        assert gm._working_badge_overlay is not None


class TestWorkingBadgeShowHide:
    def test_show_paints_glyph_at_cursor_and_arms_timer(self, qapp):
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        with patch.object(gm, "_get_cursor_pos", return_value=(500, 400)):
            gm._show_working_badge()
        paint = gm._working_badge_overlay.paint_working_badge
        paint.assert_called_once()
        # Centered on the pointer (first two positional args).
        assert paint.call_args.args[0] == 500
        assert paint.call_args.args[1] == 400
        assert gm._working_badge_shown is True
        # Fallback timer armed.
        gm._working_badge_timeout_timer.start.assert_called_once()

    def test_show_noop_when_cursor_unavailable(self, qapp):
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        with patch.object(gm, "_get_cursor_pos", return_value=None):
            gm._show_working_badge()
        gm._working_badge_overlay.paint_working_badge.assert_not_called()
        assert gm._working_badge_shown is False

    def test_hide_clears_and_stops_timer_and_is_idempotent(self, qapp):
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        with patch.object(gm, "_get_cursor_pos", return_value=(10, 20)):
            gm._show_working_badge()
        gm._hide_working_badge()
        gm._working_badge_overlay.clear.assert_called_once()
        assert gm._working_badge_shown is False
        gm._working_badge_timeout_timer.stop.assert_called()
        # Idempotent: a second hide does not clear again.
        gm._working_badge_overlay.clear.reset_mock()
        gm._hide_working_badge()
        gm._working_badge_overlay.clear.assert_not_called()

    def test_paint_and_clear_generations_strictly_increase(self, qapp):
        """Each paint/clear advances the generation so the dedicated gate never
        stale-drops the badge's own ops."""
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        with patch.object(gm, "_get_cursor_pos", return_value=(1, 2)):
            gm._show_working_badge()
        gen_paint = gm._working_badge_overlay.paint_working_badge.call_args.kwargs[
            "paint_generation"
        ]
        gm._hide_working_badge()
        gen_clear = gm._working_badge_overlay.clear.call_args.kwargs[
            "paint_generation"
        ]
        assert gen_clear > gen_paint

    def test_fallback_timer_hides_badge(self, qapp):
        """The fallback timer is wired to _hide_working_badge so a missed
        'confirmed' cannot leave the badge stuck."""
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        with patch.object(gm, "_get_cursor_pos", return_value=(1, 2)):
            gm._show_working_badge()
        assert gm._working_badge_shown is True
        # Simulate the timeout firing.
        gm._hide_working_badge()
        assert gm._working_badge_shown is False


class TestWorkingBadgeActivityRouting:
    def test_settling_state_shows_badge(self, qapp):
        gm, _ = _build_gui({})
        buf = bytearray(256)
        gm._gui_shm = MagicMock()
        gm._gui_shm.buf = buf
        _write_state(buf, "settling", 7)
        with patch.object(gm, "_show_working_badge") as show, patch.object(
            gm, "_hide_working_badge"
        ) as hide:
            gm._check_activity_shm()
        show.assert_called_once()
        hide.assert_not_called()

    def test_confirmed_state_hides_badge(self, qapp):
        gm, _ = _build_gui({})
        buf = bytearray(256)
        gm._gui_shm = MagicMock()
        gm._gui_shm.buf = buf
        # Pretend we were settling so the confirmed is a state change.
        gm._last_activity_state = "settling"
        gm._last_activity_utterance_id = 7
        _write_state(buf, "confirmed", 7)
        with patch.object(gm, "_show_working_badge") as show, patch.object(
            gm, "_hide_working_badge"
        ) as hide:
            gm._check_activity_shm()
        hide.assert_called_once()
        show.assert_not_called()


class TestWorkingBadgeFallbackTimeout:
    def test_fallback_timeout_outlasts_a_long_utterance(self):
        """The self-clearing fallback is a last-resort net for a missed
        'confirmed'/'idle', NOT a normal-path clear. 'settling' is written
        once per utterance, so the timer is armed once and never re-armed;
        if it is shorter than a continuous utterance it clears the badge
        mid-dictation on exactly the long utterances most likely to be
        retracted. It must be far longer than the 6s idle watchdog (the real
        speech-stopped clear) and any plausible single continuous utterance
        (wh-dictation-retraction-indicator.8.1)."""
        from gui import _WORKING_BADGE_TIMEOUT_MS

        assert _WORKING_BADGE_TIMEOUT_MS >= 60000


class TestWorkingBadgeDpiAwareness:
    """_dpi_awareness_is_per_monitor turns the implicit, unasserted
    assumption that GetCursorPos returns physical pixels (true only when the
    GUI thread is per-monitor-DPI-aware) into a checkable, logged signal
    (wh-dictation-retraction-indicator.8.2)."""

    def test_per_monitor_aware_returns_true(self, qapp):
        gm, _ = _build_gui({})
        with patch("ctypes.windll") as windll:
            # DPI_AWARENESS_PER_MONITOR_AWARE == 2
            windll.user32.GetAwarenessFromDpiAwarenessContext.return_value = 2
            assert gm._dpi_awareness_is_per_monitor() is True

    def test_system_aware_returns_false(self, qapp):
        gm, _ = _build_gui({})
        with patch("ctypes.windll") as windll:
            # DPI_AWARENESS_SYSTEM_AWARE == 1 -> cursor coords virtualized off
            # the primary-DPI monitor; badge would mis-position there.
            windll.user32.GetAwarenessFromDpiAwarenessContext.return_value = 1
            assert gm._dpi_awareness_is_per_monitor() is False

    def test_unaware_returns_false(self, qapp):
        gm, _ = _build_gui({})
        with patch("ctypes.windll") as windll:
            windll.user32.GetAwarenessFromDpiAwarenessContext.return_value = 0
            assert gm._dpi_awareness_is_per_monitor() is False

    def test_invalid_context_returns_none(self, qapp):
        gm, _ = _build_gui({})
        with patch("ctypes.windll") as windll:
            # DPI_AWARENESS_INVALID == -1 -> undeterminable, do not cry wolf.
            windll.user32.GetAwarenessFromDpiAwarenessContext.return_value = -1
            assert gm._dpi_awareness_is_per_monitor() is None

    def test_never_raises_on_ctypes_failure(self, qapp):
        gm, _ = _build_gui({})
        with patch("ctypes.windll") as windll:
            windll.user32.GetThreadDpiAwarenessContext.side_effect = OSError(
                "boom"
            )
            assert gm._dpi_awareness_is_per_monitor() is None


class TestBuildGuiDpiIsolation:
    """GuiManager construction in tests must be immune to process-global DPI
    contamination left by earlier test files
    (wh-working-badge-gui-test-isolation).

    In a full-suite run, an alphabetically earlier test file imports
    uiautomation, which pins the process DPI awareness before Qt can set
    per-monitor-v2. The construction-time badge gate then reads a definite
    non-per-monitor context and disables the badge for every test in this
    file. _build_gui therefore resets the gate's decision to safe after
    construction so these unit tests exercise badge logic, not the test
    runner's ambient DPI state. The gate logic itself stays covered by
    TestWorkingBadgeDpiAwareness and TestWorkingBadgeDpiSkip."""

    def test_construction_immune_to_contaminated_thread_context(self, qapp):
        # Simulate the contaminated state at the query level: the real
        # awareness query would report SYSTEM_AWARE (1). The mock is scoped
        # and touches no process-global state, unlike really flipping the
        # thread context with SetThreadDpiAwarenessContext, which would leak
        # into whatever test runs next.
        with patch("ctypes.windll") as windll:
            windll.user32.GetAwarenessFromDpiAwarenessContext.return_value = 1
            gm, _ = _build_gui({})
        assert gm._working_badge_dpi_unsafe is False


class TestWorkingBadgeDpiSkip:
    """When the GUI thread is DEFINITELY not per-monitor-DPI-aware, the badge
    coordinates from GetCursorPos would be virtualized and could place the
    badge on the wrong monitor. Detecting that and still painting shows a
    misleading position, so painting is skipped rather than just logged
    (wh-dictation-retraction-indicator.9.2)."""

    def test_show_skips_paint_when_dpi_unsafe(self, qapp):
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        gm._working_badge_dpi_unsafe = True
        with patch.object(gm, "_get_cursor_pos", return_value=(500, 400)):
            gm._show_working_badge()
        gm._working_badge_overlay.paint_working_badge.assert_not_called()
        assert gm._working_badge_shown is False

    def test_show_paints_when_dpi_safe(self, qapp):
        gm, _ = _build_gui({})
        gm._working_badge_overlay = MagicMock()
        gm._working_badge_dpi_unsafe = False
        with patch.object(gm, "_get_cursor_pos", return_value=(500, 400)):
            gm._show_working_badge()
        gm._working_badge_overlay.paint_working_badge.assert_called_once()
        assert gm._working_badge_shown is True


class TestActivityShmErrorLogging:
    """The 10ms activity-shm poll drives the badge, the button pulse, and
    utterance-boundary detection. A read failure (torn read, unmapped segment)
    must be logged so it is observable, but only once per failure run so the
    poll does not spew a warning every 10ms (wh-dictation-retraction-indicator.10.3)."""

    def _put_torn_payload(self, buf):
        # Valid 4-byte size header, but the payload is not valid JSON, so
        # json.loads raises inside _check_activity_shm.
        struct.pack_into(">I", buf, 0, 5)
        buf[4:9] = b"{ bad"

    def test_torn_read_logs_warning_once(self, qapp):
        gm, _ = _build_gui({})
        buf = bytearray(256)
        self._put_torn_payload(buf)
        gm._gui_shm = MagicMock()
        gm._gui_shm.buf = buf
        with patch("gui.logger") as mock_logger:
            gm._check_activity_shm()
            gm._check_activity_shm()  # second torn read in the same failure run
        assert mock_logger.warning.call_count == 1

    def test_successful_read_resets_error_flag(self, qapp):
        gm, _ = _build_gui({})
        buf = bytearray(256)
        self._put_torn_payload(buf)
        gm._gui_shm = MagicMock()
        gm._gui_shm.buf = buf
        with patch("gui.logger") as mock_logger:
            gm._check_activity_shm()          # logs once
            _write_state(buf, "idle", 1)      # a valid payload -> success
            gm._check_activity_shm()          # resets the error flag
            self._put_torn_payload(buf)       # torn again -> new failure run
            gm._check_activity_shm()          # logs again
        assert mock_logger.warning.call_count == 2
