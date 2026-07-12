"""Tests for the one-shot --clear-screen-reader-flag CLI shortcut (launcher.py).

A crash can leave the system-wide Windows screen-reader flag
(SPI_SETSCREENREADER) set, which disables PSReadLine in every PowerShell
session. ``python launcher.py --clear-screen-reader-flag`` is the recovery
shortcut: it clears the flag (uiParam=0) via
``utils.screen_reader_flag.clear_screen_reader_flag`` and exits 0 BEFORE any
WheelHouse process is spawned or shared memory is allocated.

These tests cover:
- the pure intent function returns True iff the flag token is in argv;
- the flag-present path calls clear and exits 0 without spawning processes;
- the flag-absent path does NOT call clear (normal startup proceeds).

A fake recording setter is injected so no real SystemParametersInfoW call is
made, and the supervisor body is replaced with a recorder so no process is
ever spawned.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import launcher


class _RecordingSetter:
    """Fake setter that records each uiParam it was called with."""

    def __init__(self, result: bool = True) -> None:
        self.calls: list[int] = []
        self._result = result

    def __call__(self, ui_param: int) -> bool:
        self.calls.append(ui_param)
        return self._result


# ---------------------------------------------------------------------------
# Pure intent function
# ---------------------------------------------------------------------------


class TestClearFlagIntent:
    def test_intent_true_when_flag_present(self):
        assert launcher._clear_screen_reader_flag_intent(
            ["launcher.py", "--clear-screen-reader-flag"]
        ) is True

    def test_intent_true_when_flag_among_other_args(self):
        assert launcher._clear_screen_reader_flag_intent(
            ["launcher.py", "--foo", "--clear-screen-reader-flag", "--bar"]
        ) is True

    def test_intent_false_when_flag_absent(self):
        assert launcher._clear_screen_reader_flag_intent(["launcher.py"]) is False

    def test_intent_false_for_empty_argv(self):
        assert launcher._clear_screen_reader_flag_intent([]) is False


# ---------------------------------------------------------------------------
# Flag-present one-shot path
# ---------------------------------------------------------------------------


class TestClearFlagOneShot:
    def test_flag_present_clears_and_exits_zero_without_spawn(self, capsys):
        """--clear-screen-reader-flag clears the flag and exits 0 with no spawn."""
        recorder = _RecordingSetter()
        spawn_marker = {"reached": False}

        def _explode(*_args, **_kwargs):
            spawn_marker["reached"] = True
            raise AssertionError("supervisor loop must not run on the clear path")

        with patch.object(launcher, "_run_supervisor", side_effect=_explode), \
             patch("launcher.multiprocessing.Process", side_effect=_explode), \
             patch("launcher.shared_memory.SharedMemory", side_effect=_explode):
            with pytest.raises(SystemExit) as exc:
                launcher.main(
                    argv=["launcher.py", "--clear-screen-reader-flag"],
                    clear_setter=recorder,
                )

        assert exc.value.code == 0
        # Clear semantics: uiParam=0 (never enables with 1).
        assert recorder.calls == [0]
        assert spawn_marker["reached"] is False
        out = capsys.readouterr().out
        assert out.strip() != ""

    def test_flag_present_exits_nonzero_when_clear_fails(self):
        """A failed best-effort clear exits 1 so the failure is observable."""
        recorder = _RecordingSetter(result=False)

        with patch.object(launcher, "_run_supervisor"):
            with pytest.raises(SystemExit) as exc:
                launcher.main(
                    argv=["launcher.py", "--clear-screen-reader-flag"],
                    clear_setter=recorder,
                )

        assert exc.value.code == 1
        assert recorder.calls == [0]

    def test_flag_present_does_not_invoke_supervisor(self):
        """The supervisor body is never entered on the clear path."""
        recorder = _RecordingSetter()

        with patch.object(launcher, "_run_supervisor") as mock_super:
            with pytest.raises(SystemExit):
                launcher.main(
                    argv=["launcher.py", "--clear-screen-reader-flag"],
                    clear_setter=recorder,
                )

        mock_super.assert_not_called()


# ---------------------------------------------------------------------------
# Flag-absent normal path
# ---------------------------------------------------------------------------


class TestClearFlagAbsent:
    def test_flag_absent_does_not_clear(self):
        """Without the flag, main() runs the supervisor and never clears."""
        recorder = _RecordingSetter()

        with patch.object(launcher, "_run_supervisor") as mock_super:
            launcher.main(argv=["launcher.py"], clear_setter=recorder)

        assert recorder.calls == []
        mock_super.assert_called_once()

    def test_flag_absent_default_argv_does_not_clear(self):
        """Default argv (no flag in this test runner) also skips the clear."""
        recorder = _RecordingSetter()

        with patch.object(launcher, "_run_supervisor") as mock_super, \
             patch.object(launcher.sys, "argv", ["launcher.py"]):
            launcher.main(clear_setter=recorder)

        assert recorder.calls == []
        mock_super.assert_called_once()


# ---------------------------------------------------------------------------
# Bootstrap: the before-multiprocessing-setup guarantee
# ---------------------------------------------------------------------------


class TestBootstrap:
    """The slice's core guarantee: the clear path runs before any
    multiprocessing setup. These tests assert it directly rather than by
    proxy, so a refactor that reordered the entry path would fail here.
    """

    def test_clear_path_skips_multiprocessing_setup(self):
        """On the clear path, freeze_support/set_start_method are NOT called."""
        with patch.object(launcher.multiprocessing, "freeze_support") as mock_fs, \
             patch.object(launcher.multiprocessing, "set_start_method") as mock_ssm, \
             patch.object(launcher, "main") as mock_main:
            launcher._bootstrap(["launcher.py", "--clear-screen-reader-flag"])

        mock_fs.assert_not_called()
        mock_ssm.assert_not_called()
        mock_main.assert_called_once()

    def test_normal_path_configures_multiprocessing_once(self):
        """On the normal path, multiprocessing is configured exactly once."""
        with patch.object(launcher.multiprocessing, "freeze_support") as mock_fs, \
             patch.object(launcher.multiprocessing, "set_start_method") as mock_ssm, \
             patch.object(launcher, "main") as mock_main:
            launcher._bootstrap(["launcher.py"])

        mock_fs.assert_called_once()
        mock_ssm.assert_called_once()
        mock_main.assert_called_once()
