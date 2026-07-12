"""Tests for ui.hwnd_utils.hwnds_match_for_foreground_compare.

The pairwise helper introduced for wh-3nwy: same-root match returns
True (the existing GA_ROOT contract); when allow_same_process=True,
two different roots can still match if both HWNDs belong to the same
process (Chromium / Electron transient helper-window case). Fail
closed on every uncertain code path.

References: wh-3nwy (Chromium post-send foreground check
false-positive), wh-fc1x (text input target handling epic),
wh-ix1z.4 (codex-review-loop round 1 design pass: pairwise helper
rather than overloading the single-arg normalizer).
"""
from unittest.mock import patch

import psutil

from ui.hwnd_utils import (
    _process_id_for_hwnd,
    hwnds_match_for_foreground_compare,
)


_MOD = "ui.hwnd_utils"


def _patch_normalize(side_effect):
    return patch(f"{_MOD}.normalize_hwnd_for_foreground_compare",
                 side_effect=side_effect)


def _patch_get_pid(mapping):
    """Patch GetWindowThreadProcessId to return PIDs from a mapping.

    mapping is hwnd -> pid; missing keys yield (0, 0). Raise an
    exception by setting the mapping value to an Exception instance.
    """
    def _fake(hwnd):
        result = mapping.get(hwnd, 0)
        if isinstance(result, Exception):
            raise result
        return (1000, result)  # (thread_id, pid)
    return patch(f"{_MOD}.win32process.GetWindowThreadProcessId",
                 side_effect=_fake)


# --- TestSameRootMatch ----------------------------------------------------


class TestSameRootMatch:
    def test_same_root_returns_true_default(self):
        with _patch_normalize(lambda h: h if h else None):
            assert hwnds_match_for_foreground_compare(1000, 1000) is True

    def test_same_root_returns_true_with_allow_same_process(self):
        with _patch_normalize(lambda h: h if h else None):
            assert hwnds_match_for_foreground_compare(
                1000, 1000, allow_same_process=True,
            ) is True


# --- TestNormalizeFailureFailsClosed --------------------------------------


class TestNormalizeFailureFailsClosed:
    def test_expected_normalize_returns_none_returns_false(self):
        with _patch_normalize(lambda h: None if h == 1000 else h):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
            ) is False

    def test_observed_normalize_returns_none_returns_false(self):
        with _patch_normalize(lambda h: None if h == 2000 else h):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
            ) is False

    def test_zero_hwnd_returns_false(self):
        with _patch_normalize(lambda h: h if h else None):
            assert hwnds_match_for_foreground_compare(0, 1000) is False
            assert hwnds_match_for_foreground_compare(1000, 0) is False
            assert hwnds_match_for_foreground_compare(0, 0) is False

    def test_none_hwnd_returns_false(self):
        with _patch_normalize(lambda h: h if h else None):
            assert hwnds_match_for_foreground_compare(None, 1000) is False
            assert hwnds_match_for_foreground_compare(1000, None) is False


# --- TestDifferentRootDefaultRejects --------------------------------------


class TestDifferentRootDefaultRejects:
    def test_different_root_returns_false_when_not_opt_in(self):
        # allow_same_process defaults to False so a different-root pair
        # is rejected even if PIDs would match.
        with _patch_normalize(lambda h: h if h else None):
            assert hwnds_match_for_foreground_compare(1000, 2000) is False


# --- TestSameProcessFallback ----------------------------------------------


class TestSameProcessFallback:
    def test_different_root_same_pid_passes_with_opt_in(self):
        # Chromium case: HWND 1000 is the main Brave window, HWND 2000
        # is the autocomplete popup. Different roots, same PID = 8888.
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 8888}):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
            ) is True

    def test_different_root_different_pid_returns_false(self):
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 9999}):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
            ) is False

    def test_different_root_zero_pid_returns_false(self):
        # GetWindowThreadProcessId returned 0 for the observed HWND.
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 0}):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
            ) is False

    def test_get_window_thread_process_id_exception_returns_false(self):
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: OSError("access denied")}):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
            ) is False


# --- TestExpectedProcessNameGuard -----------------------------------------


class TestExpectedProcessNameGuard:
    def test_process_name_matches_passes(self):
        fake_proc = type("FakeProc", (), {"name": lambda self: "brave.exe"})()
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 8888}), \
             patch.object(psutil, "Process", return_value=fake_proc):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
                expected_process_name="brave.exe",
            ) is True

    def test_process_name_case_insensitive(self):
        fake_proc = type("FakeProc", (), {"name": lambda self: "Brave.EXE"})()
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 8888}), \
             patch.object(psutil, "Process", return_value=fake_proc):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
                expected_process_name="brave.exe",
            ) is True

    def test_process_name_mismatch_returns_false(self):
        fake_proc = type("FakeProc", (), {"name": lambda self: "notepad.exe"})()
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 8888}), \
             patch.object(psutil, "Process", return_value=fake_proc):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
                expected_process_name="brave.exe",
            ) is False

    def test_psutil_no_such_process_returns_false(self):
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 8888}), \
             patch.object(psutil, "Process",
                          side_effect=psutil.NoSuchProcess(8888)):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
                expected_process_name="brave.exe",
            ) is False

    def test_psutil_value_error_returns_false(self):
        # wh-ix1z.21: psutil.Process(pid) rejects negative / non-positive
        # PIDs from stale or fake HWNDs with ValueError. The guard must
        # catch it and fail closed (the contract for uncertain cases).
        with _patch_normalize(lambda h: h if h else None), \
             _patch_get_pid({1000: 8888, 2000: 8888}), \
             patch.object(psutil, "Process",
                          side_effect=ValueError("pid must be a positive integer (got -1)")):
            assert hwnds_match_for_foreground_compare(
                1000, 2000, allow_same_process=True,
                expected_process_name="brave.exe",
            ) is False


# --- TestProcessIdHelper --------------------------------------------------


class TestProcessIdHelper:
    def test_zero_hwnd_returns_none(self):
        assert _process_id_for_hwnd(0) is None
        assert _process_id_for_hwnd(None) is None

    def test_normal_pid_returned(self):
        with _patch_get_pid({1000: 8888}):
            assert _process_id_for_hwnd(1000) == 8888

    def test_zero_pid_returns_none(self):
        with _patch_get_pid({1000: 0}):
            assert _process_id_for_hwnd(1000) is None

    def test_exception_returns_none(self):
        with _patch_get_pid({1000: OSError("denied")}):
            assert _process_id_for_hwnd(1000) is None
