"""Tests for WindowFocusManager.

WindowFocusManager is responsible for ensuring UI operations target the correct
window. It tracks window handles, restores minimized windows, and provides
fallback logic when the focused control is unavailable.

Critical for accessibility: if focus goes to the wrong window, voice-dictated
text ends up in the wrong application.
"""
import sys
import pytest
from unittest.mock import MagicMock, patch

from ui.window_focus_manager import WindowFocusManager


class TestEnsureFocused:
    """Tests for ensure_focused() -- window activation and focus logic."""

    # ========================================================================
    # Normal window focus
    # ========================================================================

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32gui")
    def test_focuses_normal_window(self, mock_win32gui, _mock_ancestor):
        """SetForegroundWindow called for a normal (non-minimized) window."""
        mock_win32gui.IsIconic.return_value = False
        mock_win32gui.GetForegroundWindow.return_value = 12345

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is True
        mock_win32gui.SetForegroundWindow.assert_called_once_with(12345)
        mock_win32gui.ShowWindow.assert_not_called()

    # ========================================================================
    # Minimized window restoration
    # ========================================================================

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32con")
    @patch("ui.window_focus_manager.win32gui")
    def test_restores_minimized_window(
        self, mock_win32gui, mock_win32con, _mock_ancestor,
    ):
        """Minimized window should be restored before focusing."""
        mock_win32gui.IsIconic.return_value = True
        mock_win32gui.GetForegroundWindow.return_value = 99999
        mock_win32con.SW_RESTORE = 9

        mgr = WindowFocusManager()
        mgr.ensure_focused(99999)

        mock_win32gui.ShowWindow.assert_called_once_with(99999, 9)
        mock_win32gui.SetForegroundWindow.assert_called_with(99999)

    # ========================================================================
    # Retry logic
    # ========================================================================

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32gui")
    def test_retries_once_when_first_attempt_misses_then_succeeds(
        self, mock_win32gui, _mock_ancestor,
    ):
        """If first GetForegroundWindow != hwnd but second == hwnd, return True."""
        mock_win32gui.IsIconic.return_value = False
        # First check: foreground is wrong; second check (post-retry): matches.
        mock_win32gui.GetForegroundWindow.side_effect = [77777, 12345]

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is True
        # SetForegroundWindow should be called twice: initial + retry.
        assert mock_win32gui.SetForegroundWindow.call_count == 2

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32gui")
    def test_returns_false_when_foreground_never_matches_target(
        self, mock_win32gui, _mock_ancestor,
    ):
        """wh-override-paste-focus-drift.1.1: SetForegroundWindow can silently
        fail on Windows when the target thread has no recent user input or
        when another app holds the foreground lock. ensure_focused must
        report False in that case so the retry handler's proceed-anyway
        branch is driven by the real refocus outcome, not by a
        no-exception-was-raised proxy.
        """

        mock_win32gui.IsIconic.return_value = False
        # Both checks: foreground stays on the wrong window.
        mock_win32gui.GetForegroundWindow.return_value = 77777

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is False
        # SetForegroundWindow tried twice (initial + retry).
        assert mock_win32gui.SetForegroundWindow.call_count == 2

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32gui")
    def test_no_retry_when_focus_achieved_first_time(
        self, mock_win32gui, _mock_ancestor,
    ):
        """If GetForegroundWindow == hwnd on first check, no retry needed."""
        mock_win32gui.IsIconic.return_value = False
        mock_win32gui.GetForegroundWindow.return_value = 12345

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is True
        mock_win32gui.SetForegroundWindow.assert_called_once_with(12345)

    # ========================================================================
    # Error handling
    # ========================================================================

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32gui")
    def test_returns_false_on_exception(self, mock_win32gui, _mock_ancestor):
        """Should return False when SetForegroundWindow raises."""
        mock_win32gui.IsIconic.return_value = False
        mock_win32gui.SetForegroundWindow.side_effect = Exception("Access denied")

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is False

    @patch("ui.hwnd_utils.win32gui.GetAncestor", side_effect=lambda h, f: h)
    @patch("ui.window_focus_manager.win32gui")
    def test_returns_false_on_is_iconic_exception(
        self, mock_win32gui, _mock_ancestor,
    ):
        """Should return False when IsIconic raises."""
        mock_win32gui.IsIconic.side_effect = Exception("Invalid handle")

        mgr = WindowFocusManager()
        result = mgr.ensure_focused(12345)

        assert result is False

    # ========================================================================
    # Zero / None HWND
    # ========================================================================

    def test_returns_false_for_zero_hwnd(self):
        """HWND of 0 is invalid -- should return False immediately."""
        mgr = WindowFocusManager()
        result = mgr.ensure_focused(0)

        assert result is False

    def test_returns_false_for_none_hwnd(self):
        """None HWND should return False immediately."""
        mgr = WindowFocusManager()
        result = mgr.ensure_focused(None)

        assert result is False


class TestEnsureFocusedSameProcessFallback:
    """wh-ensure-focused-same-process-fallback guard tests.

    Real incident shape (2026-08-21, Brave, compress command): UIA captured
    hwnd 133604, an invisible Chrome_WidgetWin_0 helper that is its own
    GA_ROOT; the visible Chrome_WidgetWin_1 frame 68996 held the
    foreground; both belong to brave.exe (pid 2584). The strict raw/root
    comparison inside ensure_focused refused, which blocked the tolerant
    check-3 comparison from ever running.

    These tests patch the Win32 seams inside ui.hwnd_utils so the REAL
    comparison chain (normalize -> strict root compare -> scoped
    same-process fallback) runs against the incident shape.
    """

    TARGET = 133604      # invisible helper window, its own GA_ROOT
    FOREGROUND = 68996   # visible browser frame, its own GA_ROOT
    BRAVE_PID = 2584

    @staticmethod
    def _identity_roots():
        return patch(
            "ui.hwnd_utils.win32gui.GetAncestor",
            side_effect=lambda hwnd, flag: hwnd,
        )

    @staticmethod
    def _pids(mapping):
        def _fake(hwnd):
            return (1000, mapping.get(hwnd, 0))
        return patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_fake,
        )

    @staticmethod
    def _process_name(name):
        p = patch("ui.hwnd_utils.psutil.Process")
        return p, name

    @staticmethod
    def _target_shape(visible, ex_style=0):
        """Pin the target-shape probes (wh-...fallback.1.1 gate).

        The incident target was an invisible toolwindow helper; the
        two-browser-windows sibling shape is a visible main frame. The
        class handles are invented, so the real Win32 probes would
        answer for a nonexistent window -- every fallback-path test
        pins them explicitly. IsWindow is pinned alive (.1.31): the
        real probe would read the invented handles as dead and the
        liveness confirmation would refuse every credit.
        """
        return patch.multiple(
            "ui.hwnd_utils.win32gui",
            IsWindowVisible=MagicMock(return_value=visible),
            GetWindowLong=MagicMock(return_value=ex_style),
            IsWindow=MagicMock(return_value=1),
        )

    # Acceptance criterion 1: the defect. Different GA_ROOTs, one process,
    # process name in the configured browser list -> True.
    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_same_process_browser_with_different_roots_returns_true(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is True

    # Acceptance criterion 2: different processes -> False.
    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_different_processes_returns_false(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: 999},
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is False

    # Acceptance criterion 3: process not in the browser list -> False.
    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_process_not_in_browser_list_returns_false(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "winword.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is False

    # wh-ensure-focused-same-process-fallback.1.34 (deepseek round 24):
    # the caller resolved the target's exe name from pid P, the helper
    # died, and Windows reused the handle for a helper of ANOTHER
    # brave.exe process (a second Brave profile, pid Q) before the
    # fallback sampled. Both fallback samples then agree on Q and the
    # exe-name gate compares strings, so every probe passed and the
    # keystrokes landed in profile B. The fallback must refuse when
    # its first sample of the target differs from the pid the caller's
    # identity sample produced.
    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_same_exe_rebind_before_fallback_sample_refuses(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """Target pid sequence: 2584 (profile A, the caller's identity
        sample), then 4444 (profile B, every fallback sample), then 0
        (the transient helper is gone when the retry re-resolves).
        The foreground always answers 4444 and psutil always answers
        brave.exe -- same exe, different process. Shapes are pinned to
        the live invisible helper so only the pid-snapshot comparison
        can refuse the fallback path."""
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        target_calls = {"n": 0}
        rebound_pid = 4444

        def _rebinding_pids(hwnd):
            if hwnd == self.TARGET:
                target_calls["n"] += 1
                if target_calls["n"] == 1:
                    return (1000, self.BRAVE_PID)
                if target_calls["n"] <= 3:
                    return (1000, rebound_pid)
                return (1000, 0)
            if hwnd == self.FOREGROUND:
                return (1000, rebound_pid)
            return (1000, 0)

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_rebinding_pids,
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is False

    # Acceptance criterion 4: normalization failure fails closed, even
    # when raw equality would have passed and the process is a listed
    # browser. GetAncestor returning 0 means "cannot compare".
    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_normalization_failure_returns_false(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        mock_wfm_gui.IsIconic.return_value = False
        # Foreground raw-equals the target; only normalization fails.
        mock_wfm_gui.GetForegroundWindow.return_value = self.TARGET
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with patch(
            "ui.hwnd_utils.win32gui.GetAncestor", return_value=0,
        ), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is False

    # Scoping guards for the constructor parameter.

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_browser_names_are_matched_case_insensitively(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """Constructor lower-cases the set, matching resolve_same_process_
        browser_names' lower-cased output and psutil's exe casing."""
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "Brave.EXE"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"BRAVE.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is True

    @patch("ui.window_focus_manager.process_identity_for_hwnd")
    @patch("ui.window_focus_manager.win32gui")
    def test_default_constructor_keeps_strict_contract(
        self, mock_wfm_gui, mock_process_name,
    ):
        """A bare WindowFocusManager() has an empty browser set: the
        same-process fallback never fires and no process lookup runs.

        The target shape is pinned to a live invisible helper (.1.31)
        so the empty-set guard is the only thing that can stop the
        fallback path -- the real probes read the invented handle as
        dead and would refuse at the shape gate, masking a dropped
        empty-set guard (seen as the M6 mutation surviving)."""
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND

        mgr = WindowFocusManager()
        with self._identity_roots(), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is False
        mock_process_name.assert_not_called()

    # wh-ensure-focused-same-process-fallback.1.1: the same-process
    # credit is scoped to targets shaped like the incident's invisible
    # helper (invisible, or WS_EX_TOOLWINDOW). A visible plain-styled
    # target is the two-browser-windows sibling shape: same PID, same
    # exe name, but a genuinely different app window -- crediting it
    # would send the keystrokes into the wrong window.

    @patch("ui.window_focus_manager.process_identity_for_hwnd")
    @patch("ui.window_focus_manager.win32gui")
    def test_visible_main_window_target_refuses_fallback(
        self, mock_wfm_gui, mock_process_name,
    ):
        """Two Brave windows: target A is a visible main frame, sibling
        B holds the foreground. The fallback must not credit, the
        SetForegroundWindow retry must run again (the pre-5785be84
        repair for a transient denial), and no process lookup runs --
        the shape gate refuses before psutil is consulted."""
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=True, ex_style=0):
            assert mgr.ensure_focused(self.TARGET) is False
        assert mock_wfm_gui.SetForegroundWindow.call_count == 2
        mock_process_name.assert_not_called()

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_visible_toolwindow_target_still_credits(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """A visible WS_EX_TOOLWINDOW target keeps the credit: helper
        surfaces (autofill, spellcheck popups) can be briefly visible
        and are still not sibling main windows."""
        from ui.hwnd_utils import WS_EX_TOOLWINDOW

        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=True, ex_style=WS_EX_TOOLWINDOW):
            assert mgr.ensure_focused(self.TARGET) is True

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_shape_probe_failure_fails_closed(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """IsWindowVisible raising (window died between the strict check
        and the fallback) refuses the credit -- uncertain answers stay
        strict, like every other probe in the chain."""
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), patch.multiple(
            "ui.hwnd_utils.win32gui",
            IsWindowVisible=MagicMock(side_effect=OSError("dead window")),
            GetWindowLong=MagicMock(return_value=0),
        ):
            assert mgr.ensure_focused(self.TARGET) is False

    # wh-ensure-focused-same-process-fallback.1.3 (codex round 2): the
    # fallback may run only after a CONFIRMED root mismatch -- both
    # normalizations succeeded and produced different roots. A
    # normalization failure in the strict compare is an uncertain
    # answer, and uncertain answers refuse. It must not fall through
    # to a fallback whose own re-normalization might succeed a moment
    # later (transient Win32 failure, or a handle reused by a new
    # window between the probes). These tests call _foreground_matches
    # directly: at the ensure_focused level the outer retry runs a
    # second, fresh evaluation, which hides the single-invocation
    # contract these tests pin.

    def _flaky_get_ancestor(self, failing_call):
        calls = {"n": 0}

        def fake(hwnd, flag):
            calls["n"] += 1
            if calls["n"] == failing_call:
                return 0
            return hwnd

        return patch(
            "ui.hwnd_utils.win32gui.GetAncestor", side_effect=fake,
        )

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_transient_target_normalization_failure_refuses(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """GetAncestor(target) fails on the strict compare and succeeds
        on every later call. The invocation must refuse instead of
        letting the fallback re-normalize its way to a credit."""
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._flaky_get_ancestor(failing_call=1), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr._foreground_matches(self.TARGET) is False

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_transient_observed_normalization_failure_refuses(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """Same contract for the observed side: GetAncestor(foreground)
        fails once (the window died or is mid-teardown), later calls
        succeed. Refuse; do not credit through the fallback."""
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._flaky_get_ancestor(failing_call=2), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr._foreground_matches(self.TARGET) is False

    # wh-ensure-focused-same-process-fallback.1.4 (codex round 3): a
    # CONFIRMED root mismatch must never be re-litigated. The earlier
    # shape handed the raw handles to hwnds_match_for_foreground_compare,
    # which normalized them AGAIN and returned True on strict root
    # equality before any PID or exe-name probe ran. Windows reuses
    # numeric HWNDs: a target destroyed after the strict compare can
    # re-normalize to the OBSERVED root, and the stale handle then
    # credits a foreground window the captured target never identified.
    # The fallback decision must rest on the raw current PIDs and the
    # expected browser exe name only -- GetAncestor runs exactly twice
    # per invocation, both times in the strict compare.

    def _rebinding_get_ancestor(self):
        """GetAncestor answers the strict compare truthfully (each
        handle is its own root, so the roots mismatch) and answers
        every LATER call with the observed root -- the handle-reuse
        rebinding from the .1.4 replay."""
        calls = {"n": 0}

        def fake(hwnd, flag):
            calls["n"] += 1
            if calls["n"] <= 2:
                return hwnd
            return self.FOREGROUND

        return patch(
            "ui.hwnd_utils.win32gui.GetAncestor", side_effect=fake,
        )

    def _rebinding_pids(self, rebound_pid):
        """First PID probe of the target sees the live helper
        (BRAVE_PID); every later probe of the target sees the process
        that reused the handle. The observed foreground always belongs
        to the reusing process."""
        calls = {"target": 0}

        def fake(hwnd):
            if hwnd == self.TARGET:
                calls["target"] += 1
                if calls["target"] == 1:
                    return (1000, self.BRAVE_PID)
                return (1000, rebound_pid)
            if hwnd == self.FOREGROUND:
                return (1000, rebound_pid)
            return (1000, 0)

        return patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=fake,
        )

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_handle_reuse_after_confirmed_mismatch_refuses(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """The .1.4 replay: the strict compare confirms a mismatch, the
        shape and process-name probes see the live invisible Brave
        helper, then the handle is destroyed and reused by a child of
        the non-Brave foreground window. Re-normalization would now
        report strict root equality; the invocation must refuse via
        the PID/exe probes instead, and must not call GetAncestor
        again after the confirmed mismatch."""
        notepad_pid = 999
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND

        def _proc(pid):
            m = MagicMock()
            m.name.return_value = (
                "brave.exe" if pid == self.BRAVE_PID else "notepad.exe"
            )
            return m

        mock_psutil_proc.side_effect = _proc

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._rebinding_get_ancestor() as mock_ancestor, \
                self._rebinding_pids(notepad_pid), \
                self._target_shape(visible=False):
            assert mgr._foreground_matches(self.TARGET) is False
            assert mock_ancestor.call_count == 2

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_fallback_credit_never_renormalizes(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """The legitimate credit (same Brave PID on both raw handles,
        brave.exe) must come from the PID/exe probes alone. GetAncestor
        is primed to rebind on any call after the strict compare; a
        third call would prove the fallback re-normalized."""
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._rebinding_get_ancestor() as mock_ancestor, self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr._foreground_matches(self.TARGET) is True
            assert mock_ancestor.call_count == 2

    # wh-ensure-focused-same-process-fallback.1.5 (codex round 4): the
    # shape gate runs before the PID and exe probes, and Windows can
    # recycle the numeric handle in between. A destroyed invisible
    # helper reborn as a VISIBLE plain-styled sibling in the same
    # Brave process passes every probe after the gate -- the exact
    # sibling shape the round-1 gate exists to refuse. The credit must
    # re-check the CURRENT handle's shape after the identity probes.

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_shape_rebind_to_visible_sibling_after_pid_probes_refuses(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """Same Brave PID and exe throughout; the handle is recycled
        by a visible plain sibling DURING the identity probes. The
        rebinding is modeled with a marker, not a fixed probe
        sequence (wh-ensure-focused-same-process-fallback.1.6): the
        TARGET's visibility answer flips to visible only once the
        fallback's own PID probes have run (the FOREGROUND-side PID
        lookup happens only inside same_process_fallback_matches). A
        shape recheck that runs BEFORE the identity probes therefore
        still sees the invisible helper and credits -- only the
        correct post-probe ordering refuses. GetAncestor must still
        run exactly twice.

        The FOREGROUND stays helper-shaped (invisible) throughout:
        since wh-ensure-focused-same-process-fallback.1.28 the pair-
        shape guard inside same_process_fallback_matches refuses a
        BOTH-visible pair on its own, which would mask the
        WindowFocusManager recheck this test exists to prove (seen
        as M24/M25 false survivors in the round-18 sweep). With the
        observed side helper-shaped, the inner guard credits and
        only the post-probe recheck on the TARGET can refuse."""
        probes = {"fallback_pid_chain_ran": False}

        def _pids_marking(hwnd):
            if hwnd == self.FOREGROUND:
                probes["fallback_pid_chain_ran"] = True
            return (1000, self.BRAVE_PID)

        def _visible(hwnd):
            # TARGET: invisible while the captured helper lives;
            # visible from the moment the identity probes have run on
            # the recycled handle. FOREGROUND: always the invisible
            # helper, so the .1.28 pair guard cannot be the refuser.
            if hwnd == self.FOREGROUND:
                return False
            return probes["fallback_pid_chain_ran"]

        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots() as mock_ancestor, patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_pids_marking,
        ), patch.multiple(
            "ui.hwnd_utils.win32gui",
            IsWindowVisible=MagicMock(side_effect=_visible),
            GetWindowLong=MagicMock(return_value=0),
            IsWindow=MagicMock(return_value=1),
        ):
            assert mgr._foreground_matches(self.TARGET) is False
            assert mock_ancestor.call_count == 2
        assert probes["fallback_pid_chain_ran"] is True, (
            "The refusal must come from the POST-probe shape recheck; "
            "if the identity probes never ran, the test proved an "
            "earlier gate instead."
        )

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_target_dies_after_pid_probes_refuses(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """wh-ensure-focused-same-process-fallback.1.31 (codex round
        20): the TARGET is destroyed after the fallback's PID probes
        run. A dead handle answers False from IsWindowVisible without
        raising, so it read as the invisible helper -- and since
        .1.28 the pair-shape gate is the LAST probe, so the dead
        handle itself became the credit. The FOREGROUND stays a live
        invisible helper throughout, so the OR credits unless a
        post-shape revalidation catches the dead TARGET. Modeled with
        the same marker style as the .1.5 rebind test: the TARGET is
        dead from the moment the fallback's FOREGROUND-side PID probe
        has run."""
        probes = {"fallback_pid_chain_ran": False}

        def _pids_marking(hwnd):
            if hwnd == self.FOREGROUND:
                probes["fallback_pid_chain_ran"] = True
            if hwnd == self.TARGET and probes["fallback_pid_chain_ran"]:
                return (1000, 0)  # dead handle: no owning process
            return (1000, self.BRAVE_PID)

        def _is_window(hwnd):
            if hwnd == self.TARGET and probes["fallback_pid_chain_ran"]:
                return 0
            return 1

        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._identity_roots(), patch(
            "ui.hwnd_utils.win32process.GetWindowThreadProcessId",
            side_effect=_pids_marking,
        ), patch.multiple(
            "ui.hwnd_utils.win32gui",
            IsWindowVisible=MagicMock(return_value=False),
            GetWindowLong=MagicMock(return_value=0),
            IsWindow=MagicMock(side_effect=_is_window),
        ):
            assert mgr._foreground_matches(self.TARGET) is False
        assert probes["fallback_pid_chain_ran"] is True, (
            "The refusal must come from the post-shape revalidation; "
            "if the identity probes never ran, the test proved an "
            "earlier gate instead."
        )

    # wh-ensure-focused-same-process-fallback.1.10 (codex round 8): the
    # per-invocation guards above all live INSIDE _foreground_matches,
    # but ensure_focused runs that check twice (initial + retry) and
    # the two invocations shared no root snapshot. Windows can recycle
    # the numeric handle between them: a destroyed helper reborn as a
    # child of the foreground sibling re-normalizes to the sibling's
    # root, and the SECOND invocation's strict equality credits it --
    # no fallback probe ever runs on a strict credit. ensure_focused
    # must resolve the target's root ONCE, up front, and every
    # invocation must refuse when the live root no longer matches
    # that snapshot.

    def _recycling_get_ancestor(self):
        """Model a numeric-handle recycle DURING ensure_focused: the
        first probe of TARGET sees the live helper (its own root);
        every later probe sees the recycled handle, now a child of
        the foreground sibling. FOREGROUND is always its own root."""
        calls = {"target": 0}

        def fake(hwnd, flag):
            if hwnd == self.TARGET:
                calls["target"] += 1
                if calls["target"] == 1:
                    return hwnd
                return self.FOREGROUND
            return hwnd

        return patch(
            "ui.hwnd_utils.win32gui.GetAncestor", side_effect=fake,
        )

    @patch("ui.hwnd_utils.psutil.Process")
    @patch("ui.window_focus_manager.win32gui")
    def test_recycle_during_refocus_refuses_strict_credit(
        self, mock_wfm_gui, mock_psutil_proc,
    ):
        """The .1.10 replay, with the fallback fully armed (configured
        browser, same PID, invisible shape) to pin that the snapshot
        refusal fires BEFORE both the strict credit and the fallback:
        the recycled handle now normalizes to the foreground sibling's
        root, so strict equality would credit the sibling, and the
        fallback probes would credit it too. Neither may run; the
        refocus must report False."""
        mock_wfm_gui.IsIconic.return_value = False
        mock_wfm_gui.GetForegroundWindow.return_value = self.FOREGROUND
        mock_psutil_proc.return_value.name.return_value = "brave.exe"

        mgr = WindowFocusManager(
            same_process_browser_names=frozenset({"brave.exe"}),
        )
        with self._recycling_get_ancestor(), self._pids(
            {self.TARGET: self.BRAVE_PID, self.FOREGROUND: self.BRAVE_PID},
        ), self._target_shape(visible=False):
            assert mgr.ensure_focused(self.TARGET) is False

    @patch("ui.window_focus_manager.win32gui")
    def test_snapshot_unresolvable_refuses_before_focus_change(
        self, mock_wfm_gui,
    ):
        """When the up-front snapshot cannot be resolved (GetAncestor
        returns 0 -- handle already destroyed), ensure_focused must
        refuse WITHOUT touching the window: no restore, no
        SetForegroundWindow. Manipulating foreground on behalf of a
        handle whose identity cannot be pinned is the fail-open the
        snapshot exists to remove."""
        mock_wfm_gui.IsIconic.return_value = False

        mgr = WindowFocusManager()
        with patch("ui.hwnd_utils.win32gui.GetAncestor", return_value=0):
            assert mgr.ensure_focused(self.TARGET) is False
        mock_wfm_gui.SetForegroundWindow.assert_not_called()
        mock_wfm_gui.ShowWindow.assert_not_called()


class TestRememberTarget:
    """Tests for remember_target() -- storing top-level HWND from UIA control."""

    # ========================================================================
    # Stores HWND from UIA control
    # ========================================================================

    def test_stores_hwnd_from_control(self):
        """Should store the NativeWindowHandle of the top-level control."""
        mock_control = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 54321
        mock_control.GetTopLevelControl.return_value = mock_top

        mgr = WindowFocusManager()
        mgr.remember_target(mock_control)

        assert mgr._last_target_hwnd == 54321

    # ========================================================================
    # None control
    # ========================================================================

    def test_handles_none_control(self):
        """None control should be a no-op (no crash, no state change)."""
        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 11111

        mgr.remember_target(None)

        # Should not change the existing stored HWND
        assert mgr._last_target_hwnd == 11111

    def test_handles_falsy_control(self):
        """Falsy control (0, empty) should be a no-op."""
        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 22222

        mgr.remember_target(0)

        assert mgr._last_target_hwnd == 22222

    # ========================================================================
    # Exception handling
    # ========================================================================

    def test_handles_exception_in_get_top_level(self):
        """Should silently handle exception from GetTopLevelControl."""
        mock_control = MagicMock()
        mock_control.GetTopLevelControl.side_effect = Exception("COM error")

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 33333

        mgr.remember_target(mock_control)

        # Should not change stored HWND on exception
        assert mgr._last_target_hwnd == 33333

    # ========================================================================
    # Edge cases
    # ========================================================================

    def test_does_not_store_zero_hwnd(self):
        """If NativeWindowHandle is 0, should not overwrite stored HWND."""
        mock_control = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 0
        mock_control.GetTopLevelControl.return_value = mock_top

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 44444

        mgr.remember_target(mock_control)

        assert mgr._last_target_hwnd == 44444

    def test_stores_when_top_level_is_none(self):
        """If GetTopLevelControl returns None, hwnd=0 so no store."""
        mock_control = MagicMock()
        mock_control.GetTopLevelControl.return_value = None

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 55555

        mgr.remember_target(mock_control)

        assert mgr._last_target_hwnd == 55555


class TestGetTargetWindow:
    """Tests for get_target_window() -- fallback logic for target window.

    Note: get_target_window() does `import uiautomation as auto` locally inside
    the function body. We patch sys.modules["uiautomation"] so the local import
    picks up the mock.
    """

    # ========================================================================
    # Uses provided control if keyboard focusable
    # ========================================================================

    def test_uses_provided_control_when_keyboard_focusable(self):
        """Should use provided control if it has IsKeyboardFocusable=True."""
        mock_auto = MagicMock()
        mock_control = MagicMock()
        mock_control.IsKeyboardFocusable = True
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 12345
        mock_control.GetTopLevelControl.return_value = mock_top

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_control)

        assert hwnd == 12345
        assert ctrl is mock_control
        mock_auto.GetFocusedControl.assert_not_called()

    # ========================================================================
    # Falls back to GetFocusedControl
    # ========================================================================

    def test_falls_back_to_get_focused_control_when_not_focusable(self):
        """Should query auto.GetFocusedControl when provided control not focusable."""
        mock_auto = MagicMock()
        mock_provided = MagicMock()
        mock_provided.IsKeyboardFocusable = False

        mock_focused = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 67890
        mock_focused.GetTopLevelControl.return_value = mock_top
        mock_auto.GetFocusedControl.return_value = mock_focused

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_provided)

        assert hwnd == 67890
        assert ctrl is mock_focused

    def test_falls_back_to_get_focused_control_when_none_provided(self):
        """Should query auto.GetFocusedControl when no control provided."""
        mock_auto = MagicMock()
        mock_focused = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 11111
        mock_focused.GetTopLevelControl.return_value = mock_top
        mock_auto.GetFocusedControl.return_value = mock_focused

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd == 11111
        assert ctrl is mock_focused

    # ========================================================================
    # Falls back to last remembered HWND
    # ========================================================================

    def test_falls_back_to_last_remembered_hwnd(self):
        """When no control yields an HWND, should use _last_target_hwnd."""
        mock_auto = MagicMock()
        mock_auto.GetFocusedControl.return_value = None

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 99999

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd == 99999
        assert ctrl is None

    def test_returns_none_hwnd_when_no_fallback(self):
        """When nothing found and no remembered HWND, returns (None, None)."""
        mock_auto = MagicMock()
        mock_auto.GetFocusedControl.return_value = None

        mgr = WindowFocusManager()
        # _last_target_hwnd defaults to None
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd is None
        assert ctrl is None

    # ========================================================================
    # GetFocusedControl exception
    # ========================================================================

    def test_handles_get_focused_control_exception(self):
        """Should fall back to remembered HWND when GetFocusedControl raises."""
        mock_auto = MagicMock()
        mock_auto.GetFocusedControl.side_effect = Exception("COM error")

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 88888

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(None)

        assert hwnd == 88888
        assert ctrl is None

    # ========================================================================
    # GetTopLevelControl exception
    # ========================================================================

    def test_handles_get_top_level_control_exception(self):
        """Should fall back to remembered HWND when GetTopLevelControl raises."""
        mock_auto = MagicMock()
        mock_focused = MagicMock()
        mock_focused.IsKeyboardFocusable = True
        mock_focused.GetTopLevelControl.side_effect = Exception("COM error")

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 77777

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_focused)

        assert hwnd == 77777
        assert ctrl is mock_focused

    # ========================================================================
    # Top-level control returns None
    # ========================================================================

    def test_falls_back_when_top_level_is_none(self):
        """If GetTopLevelControl returns None, falls back to remembered HWND."""
        mock_auto = MagicMock()
        mock_control = MagicMock()
        mock_control.IsKeyboardFocusable = True
        mock_control.GetTopLevelControl.return_value = None

        mgr = WindowFocusManager()
        mgr._last_target_hwnd = 66666

        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_control)

        assert hwnd == 66666
        assert ctrl is mock_control

    # ========================================================================
    # Control lacks IsKeyboardFocusable attribute
    # ========================================================================

    def test_falls_back_when_control_lacks_focusable_attr(self):
        """Control without IsKeyboardFocusable attr triggers fallback."""
        mock_auto = MagicMock()
        mock_provided = MagicMock(spec=[])  # no attributes
        mock_focused = MagicMock()
        mock_top = MagicMock()
        mock_top.NativeWindowHandle = 55555
        mock_focused.GetTopLevelControl.return_value = mock_top
        mock_auto.GetFocusedControl.return_value = mock_focused

        mgr = WindowFocusManager()
        with patch.dict(sys.modules, {"uiautomation": mock_auto}):
            hwnd, ctrl = mgr.get_target_window(mock_provided)

        assert hwnd == 55555
        assert ctrl is mock_focused
