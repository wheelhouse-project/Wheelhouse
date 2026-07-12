"""Tests for screen_reader_flag.py -- opt-in Windows screen-reader flag (wh-c169t).

The voice-clicking feature can opt in to setting the system-wide Windows
screen-reader flag (SPI_SETSCREENREADER) at Logic startup to speed UIA element
discovery in some apps. The flag breaks PSReadLine in every PowerShell session
on the machine, so it is opt-in and cleared on graceful shutdown.

Ownership-gated startup clear (wh-l4h.1.13): the OFF-path startup clear is no
longer unconditional. WheelHouse writes an on-disk ownership marker when it SETs
the flag and deletes it when it clears the flag. At startup the OFF-path clear
fires ONLY when the marker is present (WheelHouse set the flag and may have
crashed without clearing). When the marker is absent the setting is left
untouched -- a real screen reader (NVDA/JAWS/Narrator) may own the flag and
WheelHouse must never clear a setting it does not own. A marker-read failure
fails SAFE: it is treated as 'marker absent' so the less-harmful direction
(do nothing) is taken.

Tests inject a fake setter so they NEVER call the real SystemParametersInfoW --
the suite must produce no system-wide side effects. The marker path is also
injected (a tmp path) so the suite NEVER reads or writes the real
%APPDATA%/WheelHouse directory.

See docs/plans/2026-05-21-voice-element-clicking-design-v5.md section
'Behaviour at shutdown'.
"""

from __future__ import annotations

from unittest import mock

from utils.screen_reader_flag import (
    SPI_SETSCREENREADER,
    _set_screen_reader_flag_via_win32,
    apply_screen_reader_flag,
    clear_screen_reader_flag,
)


class _RecordingSetter:
    """Fake setter that records each uiParam it was called with."""

    def __init__(self, result: bool = True) -> None:
        self.calls: list[int] = []
        self.result = result

    def __call__(self, ui_param: int) -> bool:
        self.calls.append(ui_param)
        return self.result


class TestConstant:
    def test_spi_setscreenreader_value(self):
        # SPI_SETSCREENREADER is 0x0047 / 71 in the Windows SDK; SPI_GETSCREENREADER
        # (0x0046 / 70) is a different action and must not be confused with it.
        assert SPI_SETSCREENREADER == 0x0047
        assert SPI_SETSCREENREADER == 71


class TestApply:
    def test_enabled_true_calls_setter_with_one(self, tmp_path):
        setter = _RecordingSetter()
        marker = tmp_path / "screen_reader_flag_owned"
        result = apply_screen_reader_flag(True, setter=setter, marker_path=str(marker))
        assert setter.calls == [1]
        assert result is True

    def test_enabled_true_writes_ownership_marker(self, tmp_path):
        # wh-l4h.1.13: a successful SET records WheelHouse ownership on disk so a
        # later OFF-path startup knows it owns the flag and may clean it up.
        setter = _RecordingSetter()
        marker = tmp_path / "screen_reader_flag_owned"
        assert not marker.exists()
        apply_screen_reader_flag(True, setter=setter, marker_path=str(marker))
        assert marker.exists()

    def test_enabled_true_no_marker_when_set_fails(self, tmp_path):
        # If the syscall reports failure WheelHouse did not actually turn the flag
        # on, so it must not claim ownership.
        setter = _RecordingSetter(result=False)
        marker = tmp_path / "screen_reader_flag_owned"
        apply_screen_reader_flag(True, setter=setter, marker_path=str(marker))
        assert not marker.exists()

    def test_enabled_false_with_marker_present_clears_and_deletes_marker(
        self, tmp_path
    ):
        # wh-l4h.1.13: marker present == WheelHouse owns the flag and may have
        # crashed without clearing it. The OFF-path startup clear fires (setter(0))
        # and the marker is deleted on the successful clear.
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter()
        result = apply_screen_reader_flag(False, setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert result is True
        assert not marker.exists()

    def test_enabled_false_with_marker_absent_does_not_clear(self, tmp_path):
        # wh-l4h.1.13: marker absent == WheelHouse does NOT own the flag (a real
        # screen reader may). The OFF-path startup clear must NOT fire at all --
        # zero setter calls -- so the screen reader's flag is preserved.
        marker = tmp_path / "screen_reader_flag_owned"
        assert not marker.exists()
        setter = _RecordingSetter()
        apply_screen_reader_flag(False, setter=setter, marker_path=str(marker))
        assert setter.calls == []
        assert not marker.exists()

    def test_enabled_false_marker_read_failure_treated_as_absent(self, tmp_path):
        # wh-l4h.1.13 fail-safe: if the marker existence check raises, treat it as
        # absent (the less-harmful direction) -- do NOT clear, leaving any real
        # screen reader's flag intact.
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter()
        with mock.patch(
            "os.path.exists", side_effect=OSError("marker check failed")
        ):
            apply_screen_reader_flag(False, setter=setter, marker_path=str(marker))
        assert setter.calls == []

    def test_enabled_false_never_calls_setter_with_one(self, tmp_path):
        # Documents the reconciled intent for the downstream regression bead
        # wh-69sk8: enabled=False must never ENABLE the flag (no setter(1)).
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter()
        apply_screen_reader_flag(False, setter=setter, marker_path=str(marker))
        assert 1 not in setter.calls

    def test_setter_reporting_failure_returns_false_no_raise(self, tmp_path):
        setter = _RecordingSetter(result=False)
        marker = tmp_path / "screen_reader_flag_owned"
        result = apply_screen_reader_flag(True, setter=setter, marker_path=str(marker))
        assert result is False
        assert setter.calls == [1]

    def test_setter_raising_returns_false_no_raise(self, tmp_path):
        def _boom(ui_param: int) -> bool:
            raise OSError("SystemParametersInfoW failed")

        # Must never propagate; best-effort startup side effect.
        marker = tmp_path / "screen_reader_flag_owned"
        result = apply_screen_reader_flag(True, setter=_boom, marker_path=str(marker))
        assert result is False

    def test_setter_raising_on_clear_returns_false_no_raise(self, tmp_path):
        def _boom(ui_param: int) -> bool:
            raise OSError("SystemParametersInfoW failed")

        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        result = apply_screen_reader_flag(False, setter=_boom, marker_path=str(marker))
        assert result is False

    def test_enabled_false_failed_clear_leaves_no_stale_marker(self, tmp_path):
        # wh-9f3t.85.1: the OFF-path clear deletes the marker BEFORE calling
        # setter(0). So even when the clear reports failure (a proxy for a crash
        # right after the syscall), no stale marker survives. A stale marker would
        # let a later startup call setter(0) against a flag a real screen reader
        # set in the meantime -- the exact failure the marker exists to prevent.
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter(result=False)
        result = apply_screen_reader_flag(False, setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert not marker.exists()
        assert result is False

    def test_enabled_false_raising_clear_leaves_no_stale_marker(self, tmp_path):
        # wh-9f3t.85.1: same fail-toward-"not owned" guarantee when the clear
        # raises -- the marker is already deleted before the raising syscall.
        def _boom(ui_param: int) -> bool:
            raise OSError("SystemParametersInfoW failed")

        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        result = apply_screen_reader_flag(False, setter=_boom, marker_path=str(marker))
        assert not marker.exists()
        assert result is False


class TestClear:
    def test_clear_calls_setter_with_zero(self, tmp_path):
        setter = _RecordingSetter()
        marker = tmp_path / "screen_reader_flag_owned"
        result = clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert result is True

    def test_clear_deletes_marker(self, tmp_path):
        # wh-l4h.1.13: the clean-shutdown / CLI clear is unconditional (it always
        # calls setter(0)) and deletes the ownership marker on a successful clear
        # so the next startup does not think WheelHouse still owns the flag.
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter()
        clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert not marker.exists()

    def test_clear_unconditional_even_without_marker(self, tmp_path):
        # The clear path is NOT ownership-gated: only the startup OFF-path is.
        # Clean shutdown / CLI always clears regardless of marker presence.
        marker = tmp_path / "screen_reader_flag_owned"
        assert not marker.exists()
        setter = _RecordingSetter()
        result = clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert result is True

    def test_clear_never_calls_setter_with_one(self, tmp_path):
        setter = _RecordingSetter()
        marker = tmp_path / "screen_reader_flag_owned"
        clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert 1 not in setter.calls

    def test_clear_setter_failure_returns_false_no_raise(self, tmp_path):
        setter = _RecordingSetter(result=False)
        marker = tmp_path / "screen_reader_flag_owned"
        result = clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert result is False
        assert setter.calls == [0]

    def test_clear_setter_raising_returns_false_no_raise(self, tmp_path):
        def _boom(ui_param: int) -> bool:
            raise OSError("SystemParametersInfoW failed")

        marker = tmp_path / "screen_reader_flag_owned"
        result = clear_screen_reader_flag(setter=_boom, marker_path=str(marker))
        assert result is False

    def test_clear_failed_clear_leaves_no_stale_marker(self, tmp_path):
        # wh-9f3t.85.1: the clean-shutdown / CLI clear deletes the marker BEFORE
        # setter(0) for the same fail-toward-"not owned" reason as the OFF-path
        # clear. A failed clear (proxy for a crash right after the syscall) must
        # not leave a stale marker behind.
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter(result=False)
        result = clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert not marker.exists()
        assert result is False

    def test_clear_delete_failure_logs_error_and_still_clears(self, tmp_path):
        # wh-9f3t.86.1: if the marker delete fails for a real reason (e.g. a
        # PermissionError from an antivirus lock), it is swallowed but logged at
        # ERROR so an operator can find the resulting stale marker. The clear
        # still proceeds (setter(0) is called) -- skipping it would guarantee a
        # broken PSReadLine to avoid an unlikely stale marker, the wrong trade.
        marker = tmp_path / "screen_reader_flag_owned"
        marker.write_text("owned", encoding="utf-8")
        setter = _RecordingSetter()
        with mock.patch(
            "utils.screen_reader_flag.os.remove",
            side_effect=PermissionError("antivirus lock"),
        ), mock.patch("utils.screen_reader_flag.logger.error") as log_error:
            result = clear_screen_reader_flag(setter=setter, marker_path=str(marker))
        assert setter.calls == [0]
        assert result is True
        assert log_error.called


class TestRealWin32Setter:
    """Exercise the default ctypes path (setter=None) without a real syscall.

    The other tests inject a fake setter, so the real
    ``_set_screen_reader_flag_via_win32`` -- the production default -- would
    otherwise ship uncovered (wh-9f3t.36.3). A miswiring there (wrong action
    constant, swapped arguments, inverted BOOL check) would not be caught by
    any green test. These patch ``ctypes.WinDLL`` so the user32 call is a Mock:
    no system-wide flag is ever touched.
    """

    def test_passes_correct_args_and_returns_true_on_success(self):
        fake_user32 = mock.MagicMock()
        # SystemParametersInfoW returns a non-zero BOOL on success.
        fake_user32.SystemParametersInfoW.return_value = 1
        with mock.patch("ctypes.WinDLL", return_value=fake_user32) as win_dll:
            ok = _set_screen_reader_flag_via_win32(1)
        assert ok is True
        win_dll.assert_called_once()  # user32 loaded
        # uiAction=SPI_SETSCREENREADER, uiParam=1, pvParam=NULL, fWinIni=0.
        fake_user32.SystemParametersInfoW.assert_called_once_with(
            SPI_SETSCREENREADER, 1, None, 0,
        )

    def test_clear_passes_zero_uiparam(self):
        fake_user32 = mock.MagicMock()
        fake_user32.SystemParametersInfoW.return_value = 1
        with mock.patch("ctypes.WinDLL", return_value=fake_user32):
            ok = _set_screen_reader_flag_via_win32(0)
        assert ok is True
        fake_user32.SystemParametersInfoW.assert_called_once_with(
            SPI_SETSCREENREADER, 0, None, 0,
        )

    def test_returns_false_when_call_reports_failure(self):
        # BOOL inversion: SystemParametersInfoW returns 0 (falsy) on failure,
        # and the function must report that as False, not True.
        fake_user32 = mock.MagicMock()
        fake_user32.SystemParametersInfoW.return_value = 0
        with mock.patch("ctypes.WinDLL", return_value=fake_user32):
            ok = _set_screen_reader_flag_via_win32(1)
        assert ok is False

    def test_returns_false_no_raise_when_windll_unavailable(self):
        # A failure loading user32 (OSError) must degrade to False, not raise.
        with mock.patch("ctypes.WinDLL", side_effect=OSError("no user32")):
            ok = _set_screen_reader_flag_via_win32(1)
        assert ok is False
