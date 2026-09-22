"""Tests for ui.hwnd_utils.normalize_hwnd_for_foreground_compare (wh-oe7u.3).

The helper exists so insertion and retraction compare HWNDs through the
same root-normalization, eliminating Chromium/Electron false mismatches
where UIA exposes a renderer child HWND while GetForegroundWindow returns
the top-level frame.

Contract under test:
- 0/None input -> None (fail-closed)
- GetAncestor exception -> None (fail-closed; do not silently pass)
- GetAncestor returns 0 -> None (fail-closed)
- Valid HWND -> the GetAncestor(GA_ROOT) result
"""
import logging
from unittest.mock import MagicMock, patch

from ui.hwnd_utils import (
    GA_ROOT,
    GWL_EXSTYLE,
    WS_EX_TOOLWINDOW,
    hwnd_is_invisible_or_toolwindow,
    hwnd_no_longer_exists,
    normalize_hwnd_for_foreground_compare,
    same_process_fallback_matches,
    top_level_hwnd_from_control,
)


_MOD = "ui.hwnd_utils"


class TestNormalizeHwndForForegroundCompare:
    def test_zero_returns_none(self):
        """A zero HWND has no root; the helper must NOT call GetAncestor
        and must return None so callers fail closed."""
        assert normalize_hwnd_for_foreground_compare(0) is None

    def test_none_returns_none(self):
        assert normalize_hwnd_for_foreground_compare(None) is None

    @patch(f"{_MOD}.win32gui")
    def test_returns_root_for_child_hwnd(self, mock_win32gui):
        """Chromium-shaped case: child HWND captured by UIA, root HWND is
        the actual top-level window. The helper returns the root."""
        mock_win32gui.GetAncestor.return_value = 0xAAAA
        result = normalize_hwnd_for_foreground_compare(0xBBBB)
        assert result == 0xAAAA
        mock_win32gui.GetAncestor.assert_called_once_with(0xBBBB, GA_ROOT)

    @patch(f"{_MOD}.win32gui")
    def test_top_level_hwnd_returns_itself(self, mock_win32gui):
        """If the input is already the top-level HWND, GetAncestor(GA_ROOT)
        returns the same value -- normalized comparison is identity for
        non-child windows."""
        mock_win32gui.GetAncestor.return_value = 0xCAFE
        result = normalize_hwnd_for_foreground_compare(0xCAFE)
        assert result == 0xCAFE

    @patch(f"{_MOD}.win32gui")
    def test_get_ancestor_exception_returns_none(self, mock_win32gui):
        """GetAncestor raised (e.g. invalid HWND, dead window). Helper
        must return None so callers fail closed -- silently passing
        through the unnormalized hwnd would defeat the point."""
        mock_win32gui.GetAncestor.side_effect = OSError("invalid window handle")
        result = normalize_hwnd_for_foreground_compare(0xDEAD)
        assert result is None

    @patch(f"{_MOD}.win32gui")
    def test_get_ancestor_returns_zero_returns_none(self, mock_win32gui):
        """GetAncestor returned 0 (no ancestor / window destroyed): treat
        as failure, not as a valid 0 root."""
        mock_win32gui.GetAncestor.return_value = 0
        result = normalize_hwnd_for_foreground_compare(0xBEEF)
        assert result is None


class TestSilentNonePathsLogDebug:
    """wh-captured-target-window-lost Step 1: the four previously-silent
    None returns each log one DEBUG line naming the path, so a production
    log can say WHICH path produced the None that made verified_paste
    refuse. Success paths stay silent -- top_level_hwnd_from_control runs
    on every dictated word, so a per-call line would be noise.
    """

    def test_no_control_logs_debug(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            assert top_level_hwnd_from_control(None) is None
        assert any(
            "no control" in r.message for r in caplog.records
            if r.levelno == logging.DEBUG
        )

    def test_top_level_none_logs_debug(self, caplog):
        control = MagicMock()
        control.GetTopLevelControl.return_value = None
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            assert top_level_hwnd_from_control(control) is None
        assert any(
            "GetTopLevelControl returned nothing" in r.message
            for r in caplog.records if r.levelno == logging.DEBUG
        )

    def test_zero_native_window_handle_logs_debug(self, caplog):
        control = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = 0
        control.GetTopLevelControl.return_value = top
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            assert top_level_hwnd_from_control(control) is None
        assert any(
            "NativeWindowHandle was 0" in r.message
            for r in caplog.records if r.levelno == logging.DEBUG
        )

    @patch(f"{_MOD}.win32gui")
    def test_get_ancestor_zero_logs_debug(self, mock_win32gui, caplog):
        mock_win32gui.GetAncestor.return_value = 0
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            assert normalize_hwnd_for_foreground_compare(0xBEEF) is None
        assert any(
            "GetAncestor" in r.message and "returned 0" in r.message
            for r in caplog.records if r.levelno == logging.DEBUG
        )

    def test_success_paths_stay_silent(self, caplog):
        """No new log line on the hot success path (per-word cost)."""
        control = MagicMock()
        top = MagicMock()
        top.NativeWindowHandle = 0xCAFE
        control.GetTopLevelControl.return_value = top
        with patch(f"{_MOD}.win32gui") as mock_win32gui:
            mock_win32gui.GetAncestor.return_value = 0xCAFE
            with caplog.at_level(logging.DEBUG, logger=_MOD):
                assert top_level_hwnd_from_control(control) == 0xCAFE
                assert normalize_hwnd_for_foreground_compare(0xCAFE) == 0xCAFE
        assert not caplog.records


class TestHwndIsInvisibleOrToolwindow:
    """wh-ensure-focused-same-process-fallback.1.1 shape gate.

    The same-process foreground fallback may credit only a target shaped
    like the incident's invisible Chromium helper: invisible, or
    WS_EX_TOOLWINDOW-styled. A visible plain-styled window is the
    two-browser-windows sibling shape (same PID, same exe, different app
    window) and must answer False so callers stay strict. Every
    uncertain probe answers False (fail closed).
    """

    @patch(f"{_MOD}.win32gui")
    def test_invisible_target_is_eligible(self, mock_win32gui):
        # .1.31: an invisible answer is eligible only for a LIVE
        # window -- the liveness pin is part of the positive contract.
        mock_win32gui.IsWindowVisible.return_value = False
        mock_win32gui.IsWindow.return_value = 1
        assert hwnd_is_invisible_or_toolwindow(0x1111) is True
        mock_win32gui.GetWindowLong.assert_not_called()

    @patch(f"{_MOD}.win32gui")
    def test_visible_plain_window_is_not_eligible(self, mock_win32gui):
        mock_win32gui.IsWindowVisible.return_value = True
        mock_win32gui.GetWindowLong.return_value = 0
        assert hwnd_is_invisible_or_toolwindow(0x1111) is False
        mock_win32gui.GetWindowLong.assert_called_once_with(
            0x1111, GWL_EXSTYLE,
        )

    @patch(f"{_MOD}.win32gui")
    def test_visible_toolwindow_is_eligible(self, mock_win32gui):
        mock_win32gui.IsWindowVisible.return_value = True
        # Other ex-style bits set alongside WS_EX_TOOLWINDOW must not
        # hide the flag (bitmask test, not equality).
        mock_win32gui.GetWindowLong.return_value = WS_EX_TOOLWINDOW | 0x8
        assert hwnd_is_invisible_or_toolwindow(0x1111) is True

    @patch(f"{_MOD}.win32gui")
    def test_visible_window_with_other_exstyle_bits_is_not_eligible(
        self, mock_win32gui,
    ):
        """Non-toolwindow ex-style bits (e.g. WS_EX_TOPMOST) must not
        count -- the test is the WS_EX_TOOLWINDOW bit, not any bit."""
        mock_win32gui.IsWindowVisible.return_value = True
        mock_win32gui.GetWindowLong.return_value = 0x00040008
        assert hwnd_is_invisible_or_toolwindow(0x1111) is False

    def test_win32_constants_are_pinned(self):
        """The inlined constants must equal the Win32 values; a drifted
        constant would probe the wrong window attribute while every
        mocked test stays green."""
        assert GWL_EXSTYLE == -20
        assert WS_EX_TOOLWINDOW == 0x00000080

    @patch(f"{_MOD}.win32gui")
    def test_falsy_hwnd_is_not_eligible(self, mock_win32gui):
        """A falsy handle refuses BEFORE any Win32 probe runs. The
        probes are pinned to answer "live invisible helper" so the
        falsy guard is the only thing that can refuse -- with the
        .1.31 liveness confirmation in place, the real IsWindow(0)
        would refuse on its own and mask a dropped guard (seen as
        the M12 mutation surviving)."""
        mock_win32gui.IsWindowVisible.return_value = False
        mock_win32gui.IsWindow.return_value = 1
        assert hwnd_is_invisible_or_toolwindow(0) is False
        assert hwnd_is_invisible_or_toolwindow(None) is False
        mock_win32gui.IsWindowVisible.assert_not_called()

    @patch(f"{_MOD}.win32gui")
    def test_visibility_probe_exception_is_not_eligible(self, mock_win32gui):
        mock_win32gui.IsWindowVisible.side_effect = OSError("dead window")
        assert hwnd_is_invisible_or_toolwindow(0x1111) is False

    @patch(f"{_MOD}.win32gui")
    def test_dead_handle_is_not_eligible(self, mock_win32gui):
        """wh-ensure-focused-same-process-fallback.1.31: a destroyed
        handle answers False from IsWindowVisible WITHOUT raising, so
        it read as "invisible helper" here. Since .1.28 this probe is
        the LAST gate in same_process_fallback_matches -- no later
        probe refuses a dead handle anymore -- so the probe itself
        must confirm the handle still names a window."""
        mock_win32gui.IsWindowVisible.return_value = False
        mock_win32gui.IsWindow.return_value = 0
        assert hwnd_is_invisible_or_toolwindow(0x1111) is False

    @patch(f"{_MOD}.win32gui")
    def test_liveness_probe_exception_is_not_eligible(self, mock_win32gui):
        """An IsWindow failure is an uncertain probe: refuse (the
        .1.31 liveness confirmation must not credit on error)."""
        mock_win32gui.IsWindowVisible.return_value = False
        mock_win32gui.IsWindow.side_effect = OSError("probe died")
        assert hwnd_is_invisible_or_toolwindow(0x1111) is False

    @patch(f"{_MOD}.win32gui")
    def test_exstyle_probe_exception_is_not_eligible(self, mock_win32gui):
        mock_win32gui.IsWindowVisible.return_value = True
        mock_win32gui.GetWindowLong.side_effect = OSError("dead window")
        assert hwnd_is_invisible_or_toolwindow(0x1111) is False


class TestSameProcessFallbackMatches:
    """wh-ensure-focused-same-process-fallback.1.4: the root-free
    same-process fallback. Callers use it AFTER their own strict
    compare has confirmed a root mismatch; the helper must decide from
    the raw current PIDs and the expected exe name only, and must
    never touch GetAncestor -- re-normalizing a reused handle is
    exactly the strict-equality bypass this function exists to close.
    """

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_same_pid_and_matching_exe_matches_without_getancestor(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """Positive contract since .1.28: same PID, matching exe, AND
        a helper shape on one side (here the observed handle is the
        invisible transient helper). Shapes are pinned per-HWND
        (.1.30) -- an unconfigured MagicMock answered truthy for the
        toolwindow probe and proved nothing."""
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: hwnd != 0x2222
        )
        mock_win32gui.GetWindowLong.return_value = 0
        mock_win32gui.IsWindow.return_value = 1
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is True
        mock_win32gui.GetAncestor.assert_not_called()

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_exe_name_is_matched_case_insensitively(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """Shape pinned per-HWND (.1.30): observed side is the
        invisible helper, so the exe-name comparison is the thing
        under test."""
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.return_value.name.return_value = "Brave.EXE"
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: hwnd != 0x2222
        )
        mock_win32gui.GetWindowLong.return_value = 0
        mock_win32gui.IsWindow.return_value = 1
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is True

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_mismatched_pids_refuse(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """psutil is pinned to answer "brave.exe" and the shapes are
        pinned to a live one-sided helper pair (.1.31) so the
        PID-equality gate is the ONLY thing that can refuse here --
        an unpatched probe would refuse on its own for the fake
        handles and mask a dropped PID gate (seen as a mutation
        survivor). The PID answers are a stable per-handle mapping,
        not a consumable sequence, so the .1.31 revalidation re-probe
        sees the same values and cannot be the refuser either."""
        mock_get_pid.side_effect = (
            lambda hwnd: (1000, 2584 if hwnd == 0x1111 else 999)
        )
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: hwnd != 0x2222
        )
        mock_win32gui.GetWindowLong.return_value = 0
        mock_win32gui.IsWindow.return_value = 1
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_mismatched_exe_name_refuses(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """The .1.4 rebinding endgame: both raw handles now belong to
        the foreground process, but that process is not the browser
        the caller captured -- the exe probe must refuse. Shapes are
        pinned to a live one-sided helper pair (.1.31) so no later
        gate can mask a dropped exe probe."""
        mock_get_pid.return_value = (1000, 999)
        mock_psutil_proc.return_value.name.return_value = "notepad.exe"
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: hwnd != 0x2222
        )
        mock_win32gui.GetWindowLong.return_value = 0
        mock_win32gui.IsWindow.return_value = 1
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    def test_zero_pid_refuses(self, mock_get_pid):
        mock_get_pid.side_effect = [(1000, 0), (1000, 999)]
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    def test_pid_probe_exception_refuses(self, mock_get_pid):
        mock_get_pid.side_effect = OSError("dead window")
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    def test_falsy_hwnds_refuse(self):
        assert same_process_fallback_matches(
            0, 0x2222, expected_process_name="brave.exe",
        ) is False
        assert same_process_fallback_matches(
            0x1111, None, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_psutil_failure_refuses(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """A psutil failure on the exe probe must refuse by itself.
        Shapes are pinned to a live one-sided helper pair (.1.32,
        grok round 22) so the later .1.28/.1.31 shape gates cannot
        mask a mutant that swallows the psutil failure -- unpinned,
        the real probes read the invented handles as dead and the
        shape gate became the refuser."""
        import psutil as psutil_mod
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.side_effect = psutil_mod.NoSuchProcess(2584)
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: hwnd != 0x2222
        )
        mock_win32gui.GetWindowLong.return_value = 0
        mock_win32gui.IsWindow.return_value = 1
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False
        mock_psutil_proc.assert_called_once_with(2584)

    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_no_expected_name_matches_on_pid_equality_and_helper_shape(
        self, mock_win32gui, mock_get_pid,
    ):
        """expected_process_name=None skips only the exe probe (the
        pre-.1.4 contract of hwnds_match_for_foreground_compare,
        whose same-process tail delegates here). Since .1.28 the
        pair-shape guard still applies, so the positive fixture pins
        the observed side as the invisible helper (.1.30; the old
        name said "on_pid_equality_alone", which stopped being the
        contract)."""
        mock_get_pid.return_value = (1000, 2584)
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: hwnd != 0x2222
        )
        mock_win32gui.GetWindowLong.return_value = 0
        mock_win32gui.IsWindow.return_value = 1
        assert same_process_fallback_matches(0x1111, 0x2222) is True


class TestSameProcessFallbackPairShapeGuard:
    """wh-ensure-focused-same-process-fallback.1.28: the raw-PID and
    exe-name probes cannot tell the wh-3nwy transient-helper case from
    the two-visible-siblings case -- target A and foreground B both
    visible plain main frames of one browser process. The fallback
    must refuse the pair unless at least one side has the helper shape
    (invisible or WS_EX_TOOLWINDOW), and the shape probes run AFTER
    the identity probes (the .1.5 ordering) so the credit reflects the
    current handles.
    """

    @staticmethod
    def _win32_shapes(mock_win32gui, visible, exstyle):
        """Configure per-hwnd IsWindowVisible / GetWindowLong answers.

        IsWindow is pinned alive (.1.31): the shape probe confirms
        liveness after an invisible answer, and an unpinned mock
        would prove nothing about the credit path."""
        mock_win32gui.IsWindowVisible.side_effect = (
            lambda hwnd: visible[hwnd]
        )
        mock_win32gui.GetWindowLong.side_effect = (
            lambda hwnd, index: exstyle[hwnd]
        )
        mock_win32gui.IsWindow.return_value = 1

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_visible_plain_sibling_pair_refuses(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """Both windows visible, neither a toolwindow, same PID, listed
        exe: the exact wrong-window sibling shape. Must refuse."""
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        self._win32_shapes(
            mock_win32gui,
            visible={0x1111: True, 0x2222: True},
            exstyle={0x1111: 0, 0x2222: 0},
        )
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_visible_plain_pair_refuses_without_expected_name(
        self, mock_win32gui, mock_get_pid,
    ):
        """The expected_process_name=None path (the
        hwnds_match_for_foreground_compare same-process tail) carries
        the same guard: PID equality alone must not credit a visible
        sibling pair."""
        mock_get_pid.return_value = (1000, 2584)
        self._win32_shapes(
            mock_win32gui,
            visible={0x1111: True, 0x2222: True},
            exstyle={0x1111: 0, 0x2222: 0},
        )
        assert same_process_fallback_matches(0x1111, 0x2222) is False

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_shape_probe_failure_refuses_the_pair(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """hwnd_is_invisible_or_toolwindow answers False on a probe
        failure, so an unprobeable pair reads as two visible plain
        windows and the fallback stays strict (fail closed)."""
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        mock_win32gui.IsWindowVisible.side_effect = OSError("probe died")
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_invisible_observed_helper_still_credits(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """The wh-3nwy case: the observed foreground is an invisible
        transient helper of the same browser process. Must credit."""
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        self._win32_shapes(
            mock_win32gui,
            visible={0x1111: True, 0x2222: False},
            exstyle={0x1111: 0, 0x2222: 0},
        )
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is True

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_toolwindow_target_still_credits(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """The .1.1 incident shape: the captured target is a visible
        WS_EX_TOOLWINDOW Chromium helper. Must credit."""
        mock_get_pid.return_value = (1000, 2584)
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        self._win32_shapes(
            mock_win32gui,
            visible={0x1111: True, 0x2222: True},
            exstyle={0x1111: WS_EX_TOOLWINDOW, 0x2222: 0},
        )
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is True


class TestSameProcessFallbackDeadHandleRevalidation:
    """wh-ensure-focused-same-process-fallback.1.31 (codex round 20):
    a handle destroyed AFTER the PID and exe probes but BEFORE the
    .1.28 pair-shape gate read as "invisible helper" (IsWindowVisible
    answers False for a dead handle without raising), and nothing
    after the gate refused -- the dead handle itself became the
    affirmative helper evidence. The fallback must revalidate both
    handles after an eligible shape answer and refuse when either
    died (or was recycled to another process) since the sampling.
    """

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_expected_dies_after_identity_probes_refuses(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """The finding's concrete sequence: both PIDs sample as the
        live brave.exe pair, then the captured target dies at the
        shape probe. The observed side is a genuine live invisible
        helper, so the OR credits unless the post-shape revalidation
        catches the dead target."""
        dead = {"expected": False}

        def _pids(hwnd):
            if hwnd == 0x1111 and dead["expected"]:
                return (1000, 0)
            return (1000, 2584)

        def _visible(hwnd):
            if hwnd == 0x1111:
                dead["expected"] = True
                return False  # dead handle: False, no exception
            return False      # observed: live invisible helper

        mock_get_pid.side_effect = _pids
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        mock_win32gui.IsWindowVisible.side_effect = _visible
        mock_win32gui.IsWindow.side_effect = (
            lambda hwnd: not (hwnd == 0x1111 and dead["expected"])
        )
        mock_win32gui.GetWindowLong.return_value = 0
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False

    @patch(f"{_MOD}.psutil.Process")
    @patch(f"{_MOD}.win32process.GetWindowThreadProcessId")
    @patch(f"{_MOD}.win32gui")
    def test_observed_dies_after_identity_probes_refuses(
        self, mock_win32gui, mock_get_pid, mock_psutil_proc,
    ):
        """The other side of the finding: the observed transient
        helper dies after the PID probes. The captured target is a
        visible plain main frame, so with the dead handle no longer
        counting as a helper shape neither side is eligible."""
        dead = {"observed": False}

        def _pids(hwnd):
            if hwnd == 0x2222 and dead["observed"]:
                return (1000, 0)
            return (1000, 2584)

        def _visible(hwnd):
            if hwnd == 0x2222:
                dead["observed"] = True
                return False  # dead handle: False, no exception
            return True       # expected: visible plain main frame

        mock_get_pid.side_effect = _pids
        mock_psutil_proc.return_value.name.return_value = "brave.exe"
        mock_win32gui.IsWindowVisible.side_effect = _visible
        mock_win32gui.IsWindow.side_effect = (
            lambda hwnd: not (hwnd == 0x2222 and dead["observed"])
        )
        mock_win32gui.GetWindowLong.return_value = 0
        assert same_process_fallback_matches(
            0x1111, 0x2222, expected_process_name="brave.exe",
        ) is False


class TestHwndProvenanceTag:
    """wh-ensure-focused-same-process-fallback.1.12: window-object
    provenance marker via SetProp/GetProp.

    Every retry-path guard compares numeric handle values (normalize,
    PID, root snapshot), so Windows reusing a numeric handle for a NEW
    same-PID top-level window that is its own GA_ROOT aliases them
    all. A window property lives on the window OBJECT and dies with
    it, so a recycled numeric handle reads 0 -- the one probe a
    SAME-RUN recycle cannot alias (cross-run, a survivor marker from
    an earlier Input-process run matches only on equal 43-bit salts,
    about 2**-43 per pair of runs -- the accepted residual at
    _RUN_SALT). UIA RuntimeId was rejected for this job:
    for HWND-backed elements it is [42, hwnd], derived from the very
    handle value that recycles.
    """

    def test_tag_zero_hwnd_returns_zero(self):
        from ui.hwnd_utils import tag_hwnd_provenance
        assert tag_hwnd_provenance(0) == 0

    def test_read_zero_hwnd_returns_zero(self):
        from ui.hwnd_utils import read_hwnd_provenance
        assert read_hwnd_provenance(0) == 0

    @patch(f"{_MOD}._user32")
    def test_tag_sets_property_and_returns_marker(self, mock_user32):
        from ui.hwnd_utils import _PROVENANCE_PROP_NAME, tag_hwnd_provenance
        mock_user32.GetPropW.return_value = 0
        mock_user32.SetPropW.return_value = 1
        marker = tag_hwnd_provenance(0x12345)
        assert marker > 0
        mock_user32.SetPropW.assert_called_once_with(
            0x12345, _PROVENANCE_PROP_NAME, marker,
        )

    @patch(f"{_MOD}._user32")
    def test_tag_reuses_existing_marker_without_overwriting(
        self, mock_user32,
    ):
        """Two rejections against the same live window must not
        invalidate each other's cache entries: the second tag call
        finds the first marker already on the window object and
        returns it unchanged. Object identity is what the marker
        proves, and the existing property proves it just as well."""
        from ui.hwnd_utils import tag_hwnd_provenance
        mock_user32.GetPropW.return_value = 41
        marker = tag_hwnd_provenance(0x12345)
        assert marker == 41
        mock_user32.SetPropW.assert_not_called()

    @patch(f"{_MOD}._user32")
    def test_tag_setprop_failure_returns_zero(self, mock_user32):
        """SetProp fails against a destroyed handle and against a
        higher-integrity window (UIPI). 0 means 'no provenance
        recorded'; the retry handler refuses such entries, matching
        the .1.8 fail-closed contract."""
        from ui.hwnd_utils import tag_hwnd_provenance
        mock_user32.GetPropW.return_value = 0
        mock_user32.SetPropW.return_value = 0
        assert tag_hwnd_provenance(0x12345) == 0

    @patch(f"{_MOD}._user32")
    def test_tag_exception_returns_zero(self, mock_user32):
        from ui.hwnd_utils import tag_hwnd_provenance
        mock_user32.GetPropW.side_effect = OSError("boom")
        assert tag_hwnd_provenance(0x12345) == 0

    @patch(f"{_MOD}._user32")
    def test_read_returns_property_value(self, mock_user32):
        from ui.hwnd_utils import _PROVENANCE_PROP_NAME, read_hwnd_provenance
        mock_user32.GetPropW.return_value = 17
        assert read_hwnd_provenance(0x12345) == 17
        mock_user32.GetPropW.assert_called_once_with(
            0x12345, _PROVENANCE_PROP_NAME,
        )

    @patch(f"{_MOD}._user32")
    def test_read_absent_property_returns_zero(self, mock_user32):
        """GetProp returns NULL both for a window that was never
        tagged and for a RECYCLED handle whose new window object never
        carried the property -- the case the whole mechanism exists to
        catch."""
        from ui.hwnd_utils import read_hwnd_provenance
        mock_user32.GetPropW.return_value = None
        assert read_hwnd_provenance(0x12345) == 0

    @patch(f"{_MOD}._user32")
    def test_read_exception_returns_zero(self, mock_user32):
        from ui.hwnd_utils import read_hwnd_provenance
        mock_user32.GetPropW.side_effect = OSError("boom")
        assert read_hwnd_provenance(0x12345) == 0

    @patch(f"{_MOD}._user32")
    def test_markers_are_unique_per_call(self, mock_user32):
        """Fresh tags must never repeat within a process run: a
        recycled window tagged by a LATER rejection must read a
        DIFFERENT marker than the one stored with the earlier entry."""
        from ui.hwnd_utils import tag_hwnd_provenance
        mock_user32.GetPropW.return_value = 0
        mock_user32.SetPropW.return_value = 1
        first = tag_hwnd_provenance(0x11111)
        second = tag_hwnd_provenance(0x22222)
        assert first != second
        assert first > 0 and second > 0

    def test_distinct_salt_restart_survivor_does_not_alias_fresh_tag(self):
        """wh-ensure-focused-same-process-fallback.1.21 (codex round
        13): a window property lives on the window OBJECT, so a marker
        written by an old Input-process run survives that process's
        exit while the window stays alive. The module-level counter
        restarts with the process, so without a per-run component the
        new run's first fresh tag equals the survivor, two DIFFERENT
        windows read as proven-same at the aggregation gate, and their
        text merges. The per-run salt is what prevents that.

        Each restart is simulated with importlib.reload, which re-runs
        the module top level exactly as a new process would; the fake
        property store persists across the reloads the way real window
        properties persist across a process restart. The initial
        reload puts the "old run" at a fresh counter too -- earlier
        tests in this file advance the live module's counter, and the
        alias under test is first-tag-of-one-run against
        first-tag-of-the-next.

        .1.23/.1.25 (codex rounds 15-16): the assertion is scoped to
        runs whose salts DIFFER, so each run's salt is forced to a
        distinct known value here. Two runs that draw the SAME 43-bit
        salt still collide -- about 2**-43 per pair of runs, an
        accepted residual documented at _RUN_SALT in ui/hwnd_utils.py
        -- so no test may claim aliasing is impossible outright, and
        a test on two random draws would be flaky at exactly that
        probability."""
        import importlib

        import ui.hwnd_utils as hwnd_utils

        props = {}

        def fake_get(hwnd, name):
            return props.get(hwnd, 0)

        def fake_set(hwnd, name, value):
            props[hwnd] = value
            return 1

        def tag_in(module, salt, hwnd):
            with patch.object(module, "_user32") as mock_user32, \
                    patch.object(module, "_RUN_SALT", salt):
                mock_user32.GetPropW.side_effect = fake_get
                mock_user32.SetPropW.side_effect = fake_set
                return module.tag_hwnd_provenance(hwnd)

        try:
            old_run = importlib.reload(hwnd_utils)
            old_marker = tag_in(old_run, 3 << 20, 0xAAAA)
            assert old_marker > 0
            new_run = importlib.reload(hwnd_utils)
            fresh_marker = tag_in(new_run, 9 << 20, 0xBBBB)
            assert fresh_marker > 0
            assert props[0xAAAA] == old_marker
            assert fresh_marker != old_marker
        finally:
            importlib.reload(hwnd_utils)

    def test_exhausted_run_never_spills_into_adjacent_salt_band(self):
        """wh-ensure-focused-same-process-fallback.1.22 (codex round
        14): the .1.21 marker is salt + counter with an UNBOUNDED
        counter, so a run that tags more than 2**20 - 1 windows spills
        past its own 20-bit field into the adjacent salt band. Codex's
        concrete repro: an old run with salt 1<<20 at counter index
        (1<<20)+1 tags window A as 2,097,153; a later run that draws
        salt 2<<20 tags its FIRST window B as exactly the same value,
        the aggregation gate's marker-equality proof reads the two
        DIFFERENT windows as the same, and their text merges. The fix
        caps the per-run index: once it leaves the 20-bit field,
        tag_hwnd_provenance refuses with 0 (the existing fail-closed
        no-provenance path), so a spilled value never exists."""
        import ui.hwnd_utils as hwnd_utils

        props = {}

        def fake_get(hwnd, name):
            return props.get(hwnd, 0)

        def fake_set(hwnd, name, value):
            props[hwnd] = value
            return 1

        def tag_with(salt, index_start, hwnd):
            with patch.object(hwnd_utils, "_user32") as mock_user32, \
                    patch.object(hwnd_utils, "_RUN_SALT", salt), \
                    patch.object(hwnd_utils, "_next_marker_index",
                                 index_start):
                mock_user32.GetPropW.side_effect = fake_get
                mock_user32.SetPropW.side_effect = fake_set
                return hwnd_utils.tag_hwnd_provenance(hwnd)

        overflow_marker = tag_with(1 << 20, (1 << 20) + 1, 0xAAAA)
        assert overflow_marker == 0
        assert 0xAAAA not in props
        fresh_marker = tag_with(2 << 20, 1, 0xBBBB)
        assert fresh_marker > 0
        assert props.get(0xAAAA, 0) != props[0xBBBB]

    def test_marker_index_cap_boundary(self):
        """wh-ensure-focused-same-process-fallback.1.22: the last index
        inside the 20-bit field (2**20 - 1) still tags; the first index
        outside it (2**20) refuses with 0 and writes nothing. The exact
        boundary matters: an off-by-one toward permissiveness puts
        salt + 2**20 into the NEXT run's band."""
        import ui.hwnd_utils as hwnd_utils

        props = {}

        def fake_get(hwnd, name):
            return props.get(hwnd, 0)

        def fake_set(hwnd, name, value):
            props[hwnd] = value
            return 1

        with patch.object(hwnd_utils, "_user32") as mock_user32, \
                patch.object(hwnd_utils, "_next_marker_index",
                             (1 << 20) - 1):
            mock_user32.GetPropW.side_effect = fake_get
            mock_user32.SetPropW.side_effect = fake_set
            last_valid = hwnd_utils.tag_hwnd_provenance(0xA1)
            assert last_valid > 0
            refused = hwnd_utils.tag_hwnd_provenance(0xB2)
            assert refused == 0
            assert 0xB2 not in props

    def test_failed_write_does_not_consume_a_marker_index(self):
        """wh-ensure-focused-same-process-fallback.1.24 (codex round
        15): SetPropW fails in production -- UIPI blocks the write to
        an elevated window, and every rejection at such a window tries
        to tag it. A failed write must not burn an index: the next
        successful tag gets the SAME index the failed attempt would
        have used, so a stream of failing writes cannot drain the
        2**20 per-run marker space."""
        import ui.hwnd_utils as hwnd_utils

        props = {}

        def fake_get(hwnd, name):
            return props.get(hwnd, 0)

        calls = {"n": 0}

        def fail_first_set(hwnd, name, value):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0
            props[hwnd] = value
            return 1

        with patch.object(hwnd_utils, "_user32") as mock_user32, \
                patch.object(hwnd_utils, "_RUN_SALT", 5 << 20), \
                patch.object(hwnd_utils, "_next_marker_index", 7):
            mock_user32.GetPropW.side_effect = fake_get
            mock_user32.SetPropW.side_effect = fail_first_set
            failed = hwnd_utils.tag_hwnd_provenance(0xA1)
            assert failed == 0
            assert 0xA1 not in props
            marker = hwnd_utils.tag_hwnd_provenance(0xB2)
            assert marker == (5 << 20) + 7

    def test_failed_write_does_not_consume_the_last_usable_index(self):
        """wh-ensure-focused-same-process-fallback.1.24, codex's
        boundary repro: with the counter at the last valid index
        (2**20 - 1), a failed write consumed that index, so the NEXT
        call -- against a healthy window -- drew the refused 2**20
        index and returned 0. The failed write must leave the last
        index usable."""
        import ui.hwnd_utils as hwnd_utils

        props = {}

        def fake_get(hwnd, name):
            return props.get(hwnd, 0)

        calls = {"n": 0}

        def fail_first_set(hwnd, name, value):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0
            props[hwnd] = value
            return 1

        with patch.object(hwnd_utils, "_user32") as mock_user32, \
                patch.object(hwnd_utils, "_next_marker_index",
                             (1 << 20) - 1):
            mock_user32.GetPropW.side_effect = fake_get
            mock_user32.SetPropW.side_effect = fail_first_set
            failed = hwnd_utils.tag_hwnd_provenance(0xA1)
            assert failed == 0
            marker = hwnd_utils.tag_hwnd_provenance(0xB2)
            assert marker > 0
            assert props[0xB2] == marker


class TestHwndNoLongerExists:
    """wh-paste-target-window-vanished: the one question the paste path
    could not ask.

    normalize_hwnd_for_foreground_compare answers None for a destroyed
    handle and for three other reasons, so None cannot decide whether
    the window is gone. This helper answers that one question, and it
    answers False -- not proven gone -- whenever it cannot.
    """

    @patch(f"{_MOD}.win32gui")
    def test_a_destroyed_handle_is_proven_gone(self, mock_win32gui):
        mock_win32gui.IsWindow.return_value = False
        assert hwnd_no_longer_exists(0xABCD) is True
        mock_win32gui.IsWindow.assert_called_once_with(0xABCD)

    @patch(f"{_MOD}.win32gui")
    def test_a_live_handle_is_not_gone(self, mock_win32gui):
        mock_win32gui.IsWindow.return_value = True
        assert hwnd_no_longer_exists(0xABCD) is False

    def test_a_zero_handle_is_not_proven_gone(self):
        """A caller with no handle has no evidence, so it gets none.

        Answering True here would let a caller recover on the strength
        of never having had a target at all.
        """
        assert hwnd_no_longer_exists(0) is False

    def test_a_none_handle_is_not_proven_gone(self):
        assert hwnd_no_longer_exists(None) is False

    @patch(f"{_MOD}.win32gui")
    def test_a_failed_probe_is_not_proven_gone(self, mock_win32gui):
        mock_win32gui.IsWindow.side_effect = OSError("probe failed")
        assert hwnd_no_longer_exists(0xABCD) is False

    @patch(f"{_MOD}.win32gui")
    def test_a_failed_probe_says_so_in_the_log(self, mock_win32gui, caplog):
        mock_win32gui.IsWindow.side_effect = OSError("probe failed")
        with caplog.at_level(logging.DEBUG, logger=_MOD):
            hwnd_no_longer_exists(0xABCD)
        assert "hwnd_no_longer_exists" in caplog.text
