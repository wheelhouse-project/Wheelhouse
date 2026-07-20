"""Tests for the elevated-target pre-detection helper (wh-elevated-target-notice).

``ui.elevation_check.target_elevation_state`` compares the integrity
level of the process that owns the focused window against WheelHouse's
own integrity level, BEFORE any keystroke or clipboard write. Windows
UIPI silently discards input sent from a lower-integrity process to a
higher-integrity window (SendInput reports success either way), so
pre-detection is the only reliable signal.

Contract pinned here:

  * Target integrity RID > own RID  -> "elevated"
  * Target integrity RID <= own RID -> "not_elevated"
  * ANY failure anywhere            -> "unknown" (fail open -- the
    caller proceeds through the existing pipeline unchanged; dictation
    is never suppressed on an unproven elevation claim)
  * The check compares INTEGRITY LEVELS, never the TokenElevation
    flag: the elevation flag false-positives when WheelHouse itself
    runs elevated and on UAC-disabled machines.
  * Own RID is computed once and cached (it cannot change for the
    lifetime of the process); the target RID is computed per call
    (the focused window changes constantly -- no caching, no TOCTOU
    widening).
  * Every process/token handle opened is closed, on success and on
    the exception paths.

All Windows API access is faked by swapping the module-level win32*
names inside ``ui.elevation_check``, so these tests run without
pywin32 and without real elevated processes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import ui.elevation_check as elevation_check


# Integrity RIDs (winnt.h SECURITY_MANDATORY_*_RID).
MEDIUM_RID = 0x2000
HIGH_RID = 0x3000
LOW_RID = 0x1000

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_INTEGRITY_LEVEL = 25


class FakeSid:
    def __init__(self, rid: int):
        self._rid = rid

    def GetSubAuthorityCount(self) -> int:
        # Real integrity SIDs are S-1-16-<rid>: one sub-authority.
        return 1

    def GetSubAuthority(self, index: int) -> int:
        assert index == 0
        return self._rid


class FakeHandle:
    def __init__(self):
        self.closed = False
        # Token handles carry the RID their fake token reports.
        self.rid: int | None = None

    def Close(self) -> None:
        self.closed = True


class FakeWin32:
    """One bundle holding the fake win32api/win32security/win32process/
    win32gui/win32con namespaces plus the handles they minted, so tests
    can assert on handle lifetime."""

    def __init__(
        self,
        *,
        own_rid: int = MEDIUM_RID,
        target_rid: int = MEDIUM_RID,
        foreground_hwnd: int = 0,
    ):
        self.own_rid = own_rid
        self.target_rid = target_rid
        self.process_handles: list[FakeHandle] = []
        self.token_handles: list[FakeHandle] = []
        self.own_token_opens = 0
        self.opened_pids: list[int] = []
        self.open_process_access: list[int] = []

        bundle = self

        class _Win32Api:
            @staticmethod
            def GetCurrentProcess():
                return "pseudo-handle"

            @staticmethod
            def OpenProcess(access, inherit, pid):
                bundle.open_process_access.append(access)
                bundle.opened_pids.append(pid)
                handle = FakeHandle()
                bundle.process_handles.append(handle)
                return handle

        class _Win32Security:
            TokenIntegrityLevel = TOKEN_INTEGRITY_LEVEL

            @staticmethod
            def OpenProcessToken(process_handle, access):
                assert access == TOKEN_QUERY
                token = FakeHandle()
                if process_handle == "pseudo-handle":
                    bundle.own_token_opens += 1
                    token.rid = bundle.own_rid
                else:
                    token.rid = bundle.target_rid
                bundle.token_handles.append(token)
                return token

            @staticmethod
            def GetTokenInformation(token, info_class):
                assert info_class == TOKEN_INTEGRITY_LEVEL
                return (FakeSid(token.rid), 0)

        self.win32api = _Win32Api
        self.win32security = _Win32Security
        self.win32process = SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd: (777, 4242),
        )
        self.win32gui = SimpleNamespace(
            GetForegroundWindow=lambda: foreground_hwnd,
        )
        self.win32con = SimpleNamespace(
            PROCESS_QUERY_LIMITED_INFORMATION=PROCESS_QUERY_LIMITED_INFORMATION,
            TOKEN_QUERY=TOKEN_QUERY,
        )

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr(elevation_check, "win32api", self.win32api)
        monkeypatch.setattr(
            elevation_check, "win32security", self.win32security,
        )
        monkeypatch.setattr(
            elevation_check, "win32process", self.win32process,
        )
        monkeypatch.setattr(elevation_check, "win32gui", self.win32gui)
        monkeypatch.setattr(elevation_check, "win32con", self.win32con)


@pytest.fixture(autouse=True)
def _fresh_own_rid_cache():
    elevation_check._reset_cached_own_rid()
    yield
    elevation_check._reset_cached_own_rid()


def _control_with_hwnd(hwnd: int = 0xBEEF) -> MagicMock:
    ctrl = MagicMock()
    top = MagicMock()
    top.NativeWindowHandle = hwnd
    ctrl.GetTopLevelControl.return_value = top
    return ctrl


class TestElevationComparison:
    def test_higher_target_rid_is_elevated(self, monkeypatch):
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=HIGH_RID)
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.ELEVATED

    def test_equal_rid_is_not_elevated(self, monkeypatch):
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=MEDIUM_RID)
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.NOT_ELEVATED

    def test_lower_target_rid_is_not_elevated(self, monkeypatch):
        # A low-integrity target (sandboxed browser child) is typeable.
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=LOW_RID)
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.NOT_ELEVATED

    def test_wheelhouse_elevated_sees_high_target_as_not_elevated(
        self, monkeypatch,
    ):
        # When WheelHouse itself runs as administrator, an elevated
        # target compares equal and typing works -- exactly why the
        # comparison uses integrity levels, not the TokenElevation
        # flag (which would be true for both and prove nothing).
        fake = FakeWin32(own_rid=HIGH_RID, target_rid=HIGH_RID)
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.NOT_ELEVATED


class TestFailOpen:
    def test_get_window_thread_process_id_failure_is_unknown(
        self, monkeypatch,
    ):
        fake = FakeWin32(target_rid=HIGH_RID)

        def _boom(hwnd):
            raise OSError("invalid window handle")

        fake.win32process = SimpleNamespace(GetWindowThreadProcessId=_boom)
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.UNKNOWN

    def test_open_process_access_denied_is_unknown(self, monkeypatch):
        fake = FakeWin32(target_rid=HIGH_RID)

        class _DeniedApi(fake.win32api):
            @staticmethod
            def OpenProcess(access, inherit, pid):
                raise OSError("access denied")

        fake.win32api = _DeniedApi
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.UNKNOWN

    def test_zero_pid_is_unknown(self, monkeypatch):
        fake = FakeWin32(target_rid=HIGH_RID)
        fake.win32process = SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd: (0, 0),
        )
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.UNKNOWN

    def test_no_hwnd_anywhere_is_unknown(self, monkeypatch):
        # Control lookup fails AND GetForegroundWindow returns 0.
        fake = FakeWin32(target_rid=HIGH_RID, foreground_hwnd=0)
        fake.install(monkeypatch)
        ctrl = MagicMock()
        ctrl.GetTopLevelControl.side_effect = RuntimeError("stale com")
        state = elevation_check.target_elevation_state(ctrl)
        assert state == elevation_check.UNKNOWN

    def test_none_control_with_no_foreground_is_unknown(self, monkeypatch):
        fake = FakeWin32(target_rid=HIGH_RID, foreground_hwnd=0)
        fake.install(monkeypatch)
        assert (
            elevation_check.target_elevation_state(None)
            == elevation_check.UNKNOWN
        )

    def test_own_rid_failure_is_unknown(self, monkeypatch):
        # If WheelHouse cannot read its OWN integrity level there is
        # nothing to compare against; the check must fail open, not
        # guess.
        fake = FakeWin32(target_rid=HIGH_RID)

        class _NoTokens(fake.win32security):
            @staticmethod
            def OpenProcessToken(process_handle, access):
                if process_handle == "pseudo-handle":
                    raise OSError("cannot open own token")
                return fake.win32security.OpenProcessToken(
                    process_handle, access,
                )

        fake.win32security = _NoTokens
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.UNKNOWN


class TestHwndResolution:
    def test_falls_back_to_foreground_window(self, monkeypatch):
        # UIA visibility into elevated windows is exactly what may be
        # broken, so a failed control lookup falls back to
        # GetForegroundWindow rather than giving up.
        fake = FakeWin32(target_rid=HIGH_RID, foreground_hwnd=0xF00D)
        fake.install(monkeypatch)
        ctrl = MagicMock()
        ctrl.GetTopLevelControl.side_effect = RuntimeError("stale com")
        state = elevation_check.target_elevation_state(ctrl)
        assert state == elevation_check.ELEVATED

    def test_uses_control_hwnd_when_available(self, monkeypatch):
        seen_hwnds: list[int] = []
        fake = FakeWin32(target_rid=HIGH_RID, foreground_hwnd=0xF00D)

        def _record(hwnd):
            seen_hwnds.append(hwnd)
            return (777, 4242)

        fake.win32process = SimpleNamespace(GetWindowThreadProcessId=_record)
        fake.install(monkeypatch)
        elevation_check.target_elevation_state(_control_with_hwnd(0xBEEF))
        assert seen_hwnds == [0xBEEF]


class TestQueriesUseLimitedAccess:
    def test_open_process_asks_for_query_limited_information(
        self, monkeypatch,
    ):
        # PROCESS_QUERY_LIMITED_INFORMATION is the documented access
        # right that works across integrity levels (even on protected
        # processes). Asking for more would fail exactly on the
        # elevated targets this check exists to detect.
        fake = FakeWin32(target_rid=HIGH_RID)
        fake.install(monkeypatch)
        elevation_check.target_elevation_state(_control_with_hwnd())
        assert fake.open_process_access == [
            PROCESS_QUERY_LIMITED_INFORMATION,
        ]


class TestHandleLifetime:
    def test_success_path_closes_every_closable_handle(self, monkeypatch):
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=HIGH_RID)
        fake.install(monkeypatch)
        elevation_check.target_elevation_state(_control_with_hwnd())
        for handle in fake.process_handles:
            assert handle.closed, "target process handle leaked"
        # The own-process pseudo-handle needs no Close; the tokens do.
        for token in fake.token_handles:
            assert token.closed, "token handle leaked"

    def test_token_read_failure_still_closes_process_handle(
        self, monkeypatch,
    ):
        fake = FakeWin32(target_rid=HIGH_RID)

        class _BrokenTokenInfo(fake.win32security):
            @staticmethod
            def GetTokenInformation(token, info_class):
                if getattr(token, "rid", None) == fake.target_rid:
                    raise OSError("token read failed")
                return (FakeSid(token.rid), 0)

        fake.win32security = _BrokenTokenInfo
        fake.install(monkeypatch)
        state = elevation_check.target_elevation_state(_control_with_hwnd())
        assert state == elevation_check.UNKNOWN
        for handle in fake.process_handles:
            assert handle.closed, "process handle leaked on error path"


class TestHwndHelper:
    """``elevation_state_of_hwnd`` is the hwnd-level entry point used
    by callers that already hold a window handle (the Window
    Positioning plugin's keyboard mover; wh-winpos-silent-failure).
    Same contract: strictly-higher integrity -> ELEVATED, otherwise
    NOT_ELEVATED, any failure -> UNKNOWN."""

    def test_elevated_hwnd(self, monkeypatch):
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=HIGH_RID)
        fake.install(monkeypatch)
        assert (
            elevation_check.elevation_state_of_hwnd(0xBEEF)
            == elevation_check.ELEVATED
        )

    def test_not_elevated_hwnd(self, monkeypatch):
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=MEDIUM_RID)
        fake.install(monkeypatch)
        assert (
            elevation_check.elevation_state_of_hwnd(0xBEEF)
            == elevation_check.NOT_ELEVATED
        )

    def test_zero_hwnd_is_unknown(self, monkeypatch):
        fake = FakeWin32(target_rid=HIGH_RID)
        fake.install(monkeypatch)
        assert (
            elevation_check.elevation_state_of_hwnd(0)
            == elevation_check.UNKNOWN
        )

    def test_none_hwnd_is_unknown(self, monkeypatch):
        fake = FakeWin32(target_rid=HIGH_RID)
        fake.install(monkeypatch)
        assert (
            elevation_check.elevation_state_of_hwnd(None)
            == elevation_check.UNKNOWN
        )


class TestOwnRidCaching:
    def test_own_rid_is_computed_once_across_calls(self, monkeypatch):
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=HIGH_RID)
        fake.install(monkeypatch)
        elevation_check.target_elevation_state(_control_with_hwnd())
        elevation_check.target_elevation_state(_control_with_hwnd())
        assert fake.own_token_opens == 1

    def test_target_rid_is_computed_per_call(self, monkeypatch):
        # No caching of the target: the focused window changes between
        # insertions and a cached answer would reopen the TOCTOU gap.
        fake = FakeWin32(own_rid=MEDIUM_RID, target_rid=HIGH_RID)
        fake.install(monkeypatch)
        elevation_check.target_elevation_state(_control_with_hwnd())
        elevation_check.target_elevation_state(_control_with_hwnd())
        assert len(fake.opened_pids) == 2
