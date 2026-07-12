"""Regression test locking the wh-c169t Logic-startup screen-reader-flag contract (wh-69sk8).

The Windows screen-reader flag (SPI_SETSCREENREADER) speeds UIA element discovery
for voice clicking but breaks PSReadLine in every PowerShell session on the
machine, so it is opt-in. At Logic startup the flag is SET (uiParam=1) ONLY when
voice clicking is enabled AND the user opted in (enable_screen_reader_flag). In
EVERY other case the startup path CLEARS it (uiParam=0) as idempotent
self-recovery from a crashed session that may have left it set.

Ownership-gated OFF path (wh-l4h.1.13 supersedes the unconditional-clear
phrasing): the OFF-path startup clear now fires ONLY when WheelHouse owns the
flag, recorded by an on-disk ownership marker. With no marker present (the case
these intent tests drive), the OFF path is a NO-OP -- no setter call at all --
so a real screen reader's flag is preserved. These tests therefore assert
NO-ENABLE (uiParam=1 is never passed on any off path) and, for the marker-absent
OFF path, NO setter call at all. The guarded regression is unchanged: the flag
must NOT be set to 1 on a default-off configuration (the wh-9f3t.36.1 finding
gated on `enabled` AND opt-in, not opt-in alone).

These tests inject a fake recording setter AND a tmp (absent) ownership-marker
path so they NEVER call the real SystemParametersInfoW and NEVER touch the real
%APPDATA%/WheelHouse -- the suite must produce no system-wide side effects.
"""

from __future__ import annotations

import logging

from unittest.mock import MagicMock

import main as main_module
from main import LogicController, _screen_reader_flag_intent
from ui.click_config import ClickConfig
from utils.screen_reader_flag import apply_screen_reader_flag


class _RecordingSetter:
    """Fake setter that records each uiParam it was called with.

    Mirrors the recorder in tests/test_utils/test_screen_reader_flag.py so SET
    vs CLEAR is decided by inspecting the recorded uiParam values.
    """

    def __init__(self, result: bool = True) -> None:
        self.calls: list[int] = []
        self.result = result

    def __call__(self, ui_param: int) -> bool:
        self.calls.append(ui_param)
        return self.result


class TestScreenReaderFlagIntent:
    """Lock the startup gating: SET only on enabled AND opt-in.

    The OFF path is ownership-gated (wh-l4h.1.13): with no marker present it is a
    NO-OP. These tests inject an absent tmp marker path, so every off path asserts
    NO setter call at all (and never an enable).
    """

    def test_empty_default_config_no_enable_no_clear(self, tmp_path):
        # ClickConfig.from_raw({}) yields enabled=True, enable_screen_reader_flag=False
        # (an empty [click] block is 'flag off'). Default-off regression case: the
        # flag must NOT be set to 1. With no ownership marker the OFF path is a
        # no-op (wh-l4h.1.13) -- no setter call at all.
        cfg = ClickConfig.from_raw({})
        assert cfg.enabled is True
        assert cfg.enable_screen_reader_flag is False

        intent = _screen_reader_flag_intent(cfg)
        assert intent is False

        marker = tmp_path / "screen_reader_flag_owned"
        setter = _RecordingSetter()
        apply_screen_reader_flag(intent, setter=setter, marker_path=str(marker))
        assert setter.calls == []
        assert 1 not in setter.calls

    def test_enabled_and_opt_in_sets_flag(self, tmp_path):
        # The only case that SETs the flag: voice clicking on AND user opted in.
        cfg = ClickConfig.from_raw(
            {"enabled": True, "enable_screen_reader_flag": True}
        )
        assert cfg.enabled is True
        assert cfg.enable_screen_reader_flag is True

        intent = _screen_reader_flag_intent(cfg)
        assert intent is True

        marker = tmp_path / "screen_reader_flag_owned"
        setter = _RecordingSetter()
        apply_screen_reader_flag(intent, setter=setter, marker_path=str(marker))
        assert setter.calls == [1]

    def test_global_opt_out_with_opt_in_no_enable_no_clear(self, tmp_path):
        # The wh-9f3t.36.1 case: enabled=false + enable_screen_reader_flag=true is
        # valid config that survives validation. Gating on opt-in ALONE would
        # wrongly SET the flag; gating on enabled AND opt-in correctly does NOT set
        # it. With no ownership marker the OFF path is a no-op (wh-l4h.1.13).
        cfg = ClickConfig.from_raw(
            {"enabled": False, "enable_screen_reader_flag": True}
        )
        assert cfg.enabled is False
        assert cfg.enable_screen_reader_flag is True

        intent = _screen_reader_flag_intent(cfg)
        assert intent is False

        marker = tmp_path / "screen_reader_flag_owned"
        setter = _RecordingSetter()
        apply_screen_reader_flag(intent, setter=setter, marker_path=str(marker))
        assert setter.calls == []
        assert 1 not in setter.calls

    def test_enabled_without_opt_in_no_enable_no_clear(self, tmp_path):
        # Voice clicking on but opt-in off: the common default-ish case. With no
        # ownership marker the OFF path is a no-op (wh-l4h.1.13) -- no setter call.
        cfg = ClickConfig.from_raw(
            {"enabled": True, "enable_screen_reader_flag": False}
        )
        assert cfg.enabled is True
        assert cfg.enable_screen_reader_flag is False

        intent = _screen_reader_flag_intent(cfg)
        assert intent is False

        marker = tmp_path / "screen_reader_flag_owned"
        setter = _RecordingSetter()
        apply_screen_reader_flag(intent, setter=setter, marker_path=str(marker))
        assert setter.calls == []
        assert 1 not in setter.calls


class _ApplyRecorder:
    """Records the bool passed to apply_screen_reader_flag at the call site."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def __call__(self, enabled: bool) -> bool:
        self.calls.append(enabled)
        return True


class TestStartupCallSiteWiring:
    """Guard the call-site wiring in LogicController._apply_startup_screen_reader_flag.

    The isolated tests above validate the gate function and apply helper, but a
    regression that left _screen_reader_flag_intent correct while changing the
    call site back to opt-in-alone (self._screen_reader_flag_enabled =
    click_cfg.enable_screen_reader_flag) would pass them all yet SET uiParam=1
    for the enabled=false + opt-in=true case in production. These tests drive the
    actual config -> intent -> assign -> apply wiring on the method, monkeypatching
    apply_screen_reader_flag in main's namespace so no real syscall fires
    (wh-9f3t.40.1).
    """

    def _build_controller(self, raw_click: dict) -> LogicController:
        ctrl = LogicController.__new__(LogicController)
        ctrl.config_service = MagicMock()
        ctrl.config_service.get.return_value = raw_click
        return ctrl

    def test_disabled_with_opt_in_clears_never_enables(self, monkeypatch):
        # The wh-9f3t.36.1 / .40.1 regression case: opt-in-alone wiring would
        # wrongly enable here. Correct wiring assigns False and CLEARS.
        recorder = _ApplyRecorder()
        monkeypatch.setattr(main_module, "apply_screen_reader_flag", recorder)
        ctrl = self._build_controller(
            {"enabled": False, "enable_screen_reader_flag": True}
        )

        ctrl._apply_startup_screen_reader_flag()

        assert ctrl._screen_reader_flag_enabled is False
        assert recorder.calls == [False]

    def test_enabled_with_opt_in_sets_flag(self, monkeypatch):
        recorder = _ApplyRecorder()
        monkeypatch.setattr(main_module, "apply_screen_reader_flag", recorder)
        ctrl = self._build_controller(
            {"enabled": True, "enable_screen_reader_flag": True}
        )

        ctrl._apply_startup_screen_reader_flag()

        assert ctrl._screen_reader_flag_enabled is True
        assert recorder.calls == [True]

    def test_empty_default_config_clears(self, monkeypatch):
        recorder = _ApplyRecorder()
        monkeypatch.setattr(main_module, "apply_screen_reader_flag", recorder)
        ctrl = self._build_controller({})

        ctrl._apply_startup_screen_reader_flag()

        assert ctrl._screen_reader_flag_enabled is False
        assert recorder.calls == [False]

    def test_off_path_log_does_not_claim_a_clear_happened(
        self, monkeypatch, caplog
    ):
        # wh-l4h.1.16: with the ownership marker absent (the common case) the
        # OFF path is a no-op, so the INFO line must not assert the flag was
        # CLEARED -- that contradicts the module's own "startup clear skipped"
        # DEBUG line and misleads screen-reader-flag debugging.
        recorder = _ApplyRecorder()
        monkeypatch.setattr(main_module, "apply_screen_reader_flag", recorder)
        ctrl = self._build_controller({})

        with caplog.at_level(logging.INFO):
            ctrl._apply_startup_screen_reader_flag()

        flag_msgs = [
            r.getMessage()
            for r in caplog.records
            if "Screen-reader flag" in r.getMessage()
        ]
        assert flag_msgs, "expected an INFO line about the flag on the OFF path"
        assert all("CLEARED" not in m for m in flag_msgs)
        assert any(
            "cleared only if the ownership marker is present" in m
            for m in flag_msgs
        )
