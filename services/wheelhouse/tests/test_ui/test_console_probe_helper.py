"""Tests for the console-probe helper subprocess entry point.

The helper is the ONLY component that calls AttachConsole/GetConsoleMode. It
runs in its own isolated process so the Logic process never binds to a foreign
terminal's console. These tests exercise the helper's pure request/response
loop and its at-prompt logic against mocked psutil + Win32 bindings, so they
run on any platform.

The helper:
  * reads newline-delimited JSON requests {"pid": <int>} from a stream
  * writes newline-delimited JSON responses {"pid": <int>, "result": <bool>}
  * handles all psutil/COM errors internally and returns result:false
  * never writes to stderr (the launcher discards it at the OS level too)
"""

import io
import json
from unittest.mock import MagicMock, patch

import psutil
import pytest

from ui import console_probe_helper as helper

_RealProcess = psutil.Process


def _mock_process(name, pid, children=None):
    proc = MagicMock(spec=_RealProcess)
    proc.name.return_value = name
    proc.pid = pid
    proc.children.return_value = children or []
    return proc


class TestHelperLoop:
    def test_loop_reads_request_writes_response(self):
        # Two requests, then EOF. Each gets a JSON response carrying its pid.
        req = (
            json.dumps({"pid": 100}) + "\n"
            + json.dumps({"pid": 200}) + "\n"
        )
        stdin = io.StringIO(req)
        stdout = io.StringIO()

        with patch.object(helper, "probe_at_prompt", side_effect=[True, False]):
            helper.run_loop(stdin, stdout)

        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first == {"pid": 100, "result": True}
        assert second == {"pid": 200, "result": False}

    def test_loop_bad_json_request_yields_false_response(self):
        stdin = io.StringIO("not-json\n")
        stdout = io.StringIO()

        helper.run_loop(stdin, stdout)

        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        # A malformed request must still produce a graceful response, not crash.
        assert len(lines) == 1
        resp = json.loads(lines[0])
        assert resp["result"] is False

    def test_loop_probe_exception_yields_false(self):
        stdin = io.StringIO(json.dumps({"pid": 100}) + "\n")
        stdout = io.StringIO()

        with patch.object(helper, "probe_at_prompt", side_effect=RuntimeError("boom")):
            helper.run_loop(stdin, stdout)

        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        assert len(lines) == 1
        resp = json.loads(lines[0])
        assert resp == {"pid": 100, "result": False}

    def test_response_channel_corrupted_exits_without_writing(self):
        # A failed restore of fd 1 makes stdout possibly foreign. The loop must
        # NOT write the response (that would leak into the user's terminal) and
        # must exit so the client recycles the helper (wh-jvrs.2.3).
        stdin = io.StringIO(
            json.dumps({"pid": 100}) + "\n"
            + json.dumps({"pid": 200}) + "\n"
        )
        stdout = io.StringIO()

        with patch.object(
            helper,
            "probe_at_prompt",
            side_effect=helper.ResponseChannelCorrupted("fd 1 restore failed"),
        ):
            helper.run_loop(stdin, stdout)

        # Nothing written to the (possibly foreign) stdout, and the loop bailed
        # after the first request rather than processing the second.
        assert stdout.getvalue() == ""


class TestHelperProbeLogic:
    """The helper owns the same at-prompt logic the detector used to."""

    @patch("ui.console_probe_helper.psutil.Process")
    def test_shell_no_children_is_at_prompt(self, mock_process_cls):
        pwsh = _mock_process("pwsh.exe", 200)
        terminal = _mock_process("WindowsTerminal.exe", 100, children=[pwsh])
        mock_process_cls.return_value = terminal

        assert helper.probe_at_prompt(100) is True

    @patch("ui.console_probe_helper.psutil.Process")
    def test_no_such_process_returns_false(self, mock_process_cls):
        mock_process_cls.side_effect = psutil.NoSuchProcess(999)

        assert helper.probe_at_prompt(999) is False

    @patch("ui.console_probe_helper.psutil.Process")
    def test_legacy_console_running_command_returns_false(self, mock_process_cls):
        ping = _mock_process("ping.exe", 200)
        cmd = _mock_process("cmd.exe", 100, children=[ping])
        mock_process_cls.return_value = cmd

        assert helper.probe_at_prompt(100) is False

    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_interactive_child_raw_mode_returns_true(self, mock_process_cls, mock_k32):
        claude = _mock_process("claude.exe", 300)
        cmd = _mock_process("cmd.exe", 200, children=[claude])
        terminal = _mock_process("WindowsTerminal.exe", 100, children=[cmd])
        mock_process_cls.return_value = terminal

        mock_k32.AttachConsole.return_value = True

        def set_mode(handle, mode_ptr):
            mode_ptr._obj.value = 0x0200  # VT input only, no LINE_INPUT
            return True

        mock_k32.GetConsoleMode.side_effect = set_mode

        assert helper.probe_at_prompt(100) is True

    @patch("ui.console_probe_helper._has_interactive_child")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_multi_tab_one_busy_after_interactive_returns_false(
        self, mock_process_cls, mock_has_interactive
    ):
        # wh-jvrs.4.1: two shells in one terminal. Tab 1 runs an interactive
        # TUI (vim -> raw mode); tab 2 runs a blocking command (git clone ->
        # cooked mode). _find_shells returns BOTH. The loop must not return True
        # on the first (interactive) shell and mask the second (busy) shell --
        # any busy tab means the terminal is not safely at a prompt.
        vim = _mock_process("vim.exe", 301)
        pwsh_tab1 = _mock_process("pwsh.exe", 201, children=[vim])
        git = _mock_process("git.exe", 302)
        pwsh_tab2 = _mock_process("pwsh.exe", 202, children=[git])
        terminal = _mock_process(
            "WindowsTerminal.exe", 100, children=[pwsh_tab1, pwsh_tab2]
        )
        mock_process_cls.return_value = terminal

        # Interactive only for tab 1's shell pid; tab 2 is busy (cooked mode).
        mock_has_interactive.side_effect = lambda pid: pid == 201

        # Even though tab 1 (processed first) is interactive, the busy tab 2
        # must drive the verdict to False.
        assert helper.probe_at_prompt(100) is False

    @patch("ui.console_probe_helper._has_interactive_child")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_multi_tab_all_idle_or_tui_returns_true(
        self, mock_process_cls, mock_has_interactive
    ):
        # wh-jvrs.4.1 / wh-jvrs.4.2: two tabs, one idle (no children) and one
        # running an interactive TUI. No busy (cooked-mode) shell anywhere, so
        # the terminal IS safely at a prompt and the loop returns True only
        # after verifying every shell.
        vim = _mock_process("vim.exe", 301)
        pwsh_tab1 = _mock_process("pwsh.exe", 201, children=[vim])
        pwsh_tab2 = _mock_process("pwsh.exe", 202, children=[])  # idle tab
        terminal = _mock_process(
            "WindowsTerminal.exe", 100, children=[pwsh_tab1, pwsh_tab2]
        )
        mock_process_cls.return_value = terminal

        mock_has_interactive.return_value = True  # the only shell with children

        assert helper.probe_at_prompt(100) is True

    @patch("ui.console_probe_helper.psutil.Process")
    def test_access_denied_on_construction_returns_false(self, mock_process_cls):
        # wh-jvrs.4.2: AccessDenied on psutil.Process(...) must fail closed to
        # False, the same conservative posture as NoSuchProcess.
        mock_process_cls.side_effect = psutil.AccessDenied(999)

        assert helper.probe_at_prompt(999) is False

    @patch("ui.console_probe_helper.psutil.Process")
    def test_access_denied_on_name_returns_false(self, mock_process_cls):
        # wh-jvrs.4.2: AccessDenied when reading proc.name() must fail closed.
        proc = MagicMock(spec=_RealProcess)
        proc.name.side_effect = psutil.AccessDenied(100)
        mock_process_cls.return_value = proc

        assert helper.probe_at_prompt(100) is False

    @patch("ui.console_probe_helper.psutil.Process")
    def test_terminal_with_only_intermediary_no_shell_returns_false(
        self, mock_process_cls
    ):
        # wh-jvrs.4.2: a terminal whose subtree contains only intermediary
        # processes (conhost/openconsole) and no shell yields an empty
        # _find_shells result -> the `if not shells: return False` branch.
        conhost = _mock_process("conhost.exe", 200, children=[])
        terminal = _mock_process(
            "WindowsTerminal.exe", 100, children=[conhost]
        )
        mock_process_cls.return_value = terminal

        assert helper.probe_at_prompt(100) is False

    @patch("ui.console_probe_helper.psutil.Process")
    def test_legacy_console_cmd_at_prompt_no_children_returns_true(
        self, mock_process_cls
    ):
        # wh-jvrs.4.2: legacy console where the shell (cmd.exe) IS the
        # top-level process and has no children -> at a prompt (True). This is
        # the True companion of test_legacy_console_running_command_returns_false.
        cmd = _mock_process("cmd.exe", 100, children=[])
        mock_process_cls.return_value = cmd

        assert helper.probe_at_prompt(100) is True


class TestHelperConsoleMode:
    """Dedicated regression guards for the AttachConsole/GetConsoleMode branches
    inside ``_has_interactive_child`` (wh-jvrs.4.2).

    These branches were previously exercised only implicitly through the
    catch-all conservative-return-False paths. Each test pins one branch so a
    future edit that swaps result polarity or mishandles the
    GetConsoleMode-failure cleanup is caught.
    """

    def _terminal_with_busy_shell(self, mock_process_cls):
        # A shell with a child so probe_at_prompt reaches _has_interactive_child.
        child = _mock_process("git.exe", 300)
        shell = _mock_process("pwsh.exe", 200, children=[child])
        terminal = _mock_process(
            "WindowsTerminal.exe", 100, children=[shell]
        )
        mock_process_cls.return_value = terminal

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_attach_console_failure_returns_false(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # AttachConsole returning False (e.g. ConPTY) -> not at prompt (False).
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = False  # attach fails

        assert helper.probe_at_prompt(100) is False
        # GetConsoleMode is never reached when the attach fails.
        mock_k32.GetConsoleMode.assert_not_called()

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_get_console_mode_failure_returns_false(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # GetConsoleMode returning False -> conservative False, and the inner
        # finally still detaches via FreeConsole (cleanup not skipped).
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = True
        conin_handle = 0xC0
        mock_k32.CreateFileW.return_value = conin_handle
        mock_k32.GetConsoleMode.return_value = False  # mode read fails

        assert helper.probe_at_prompt(100) is False
        # The CONIN$ handle is still closed when GetConsoleMode fails -- the
        # CloseHandle runs in the inner finally, not only on the success path.
        mock_k32.CloseHandle.assert_called_once_with(conin_handle)
        # FreeConsole is called at least twice: the pre-attach detach and the
        # inner finally after the failed GetConsoleMode (cleanup preserved).
        assert mock_k32.FreeConsole.call_count >= 2

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_cooked_mode_noninteractive_child_returns_false(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # ENABLE_LINE_INPUT set (cooked mode) -> blocking non-interactive
        # command, not an interactive TUI -> False. This pins the result
        # polarity of `result = not (mode & ENABLE_LINE_INPUT)`.
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = True

        def set_cooked_mode(handle, mode_ptr):
            # ENABLE_LINE_INPUT (0x0002) ON == cooked == non-interactive.
            mode_ptr._obj.value = helper._ENABLE_LINE_INPUT
            return True

        mock_k32.GetConsoleMode.side_effect = set_cooked_mode

        assert helper.probe_at_prompt(100) is False

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_redirected_std_input_reads_mode_via_conin(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # Regression for the terminal editor failing to open (wh-jvrs follow-up):
        # the helper runs with stdin/stdout redirected to pipes for its JSON
        # protocol. AttachConsole does NOT overwrite std handles that were
        # redirected at process creation, so GetStdHandle(STD_INPUT_HANDLE)
        # returns the helper's stdin PIPE and GetConsoleMode FAILS on it
        # (ERROR_INVALID_HANDLE). The console input mode must therefore be read
        # from the console's own CONIN$ buffer, which AttachConsole always
        # binds, never from GetStdHandle.
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = True

        conin_handle = 0xC0  # the CONIN$ console-input handle CreateFileW returns
        mock_k32.CreateFileW.return_value = conin_handle

        def get_mode(handle, mode_ptr):
            # GetConsoleMode must be called on the CONIN$ handle. A revert to
            # GetStdHandle would pass the redirected stdin pipe instead, on which
            # GetConsoleMode fails with ERROR_INVALID_HANDLE -- modelled here as a
            # False return for any handle that is not the CONIN$ one.
            if handle == conin_handle:
                mode_ptr._obj.value = 0x0200  # raw mode: ENABLE_LINE_INPUT off
                return True
            return False

        mock_k32.GetConsoleMode.side_effect = get_mode

        # Interactive TUI (raw mode) read via CONIN$ -> the shell is at a usable
        # prompt and probe_at_prompt returns True. A revert to the GetStdHandle
        # read returns False because GetConsoleMode fails on the pipe handle.
        assert helper.probe_at_prompt(100) is True

        # Pin the CONIN$ open contract: read-only access to the console INPUT
        # buffer. A typo to "CONOUT$" reads the wrong buffer; an added
        # _GENERIC_WRITE would grant write access to a foreign console -- the
        # exact leak this helper subprocess exists to prevent. Either drift
        # changes these arguments and fails here.
        mock_k32.CreateFileW.assert_called_once_with(
            "CONIN$",
            helper._GENERIC_READ,
            helper._FILE_SHARE_READ | helper._FILE_SHARE_WRITE,
            None,
            helper._OPEN_EXISTING,
            0,
            None,
        )

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_createfile_invalid_handle_returns_false(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # CreateFileW("CONIN$") returning INVALID_HANDLE_VALUE -> conservative
        # False, and neither GetConsoleMode nor CloseHandle runs (there is no
        # valid handle to read or close).
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = True
        mock_k32.CreateFileW.return_value = helper._INVALID_HANDLE_VALUE

        assert helper.probe_at_prompt(100) is False
        mock_k32.GetConsoleMode.assert_not_called()
        mock_k32.CloseHandle.assert_not_called()

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_createfile_null_handle_returns_false(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # CreateFileW returning NULL (ctypes surfaces a 0/NULL HANDLE as None)
        # -> conservative False; the `not conin` arm of the guard catches it.
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = True
        mock_k32.CreateFileW.return_value = None

        assert helper.probe_at_prompt(100) is False
        mock_k32.GetConsoleMode.assert_not_called()
        mock_k32.CloseHandle.assert_not_called()

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    @patch("ui.console_probe_helper.psutil.Process")
    def test_conin_handle_closed_after_successful_read(
        self, mock_process_cls, mock_k32, mock_os
    ):
        # On a successful read the CONIN$ handle must be closed exactly once,
        # with the handle CreateFileW returned -- no handle leak per probe.
        self._terminal_with_busy_shell(mock_process_cls)
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_os.dup2.return_value = 0
        mock_k32.AttachConsole.return_value = True
        conin_handle = 0xC0
        mock_k32.CreateFileW.return_value = conin_handle

        def set_raw_mode(handle, mode_ptr):
            mode_ptr._obj.value = 0x0200  # raw mode: ENABLE_LINE_INPUT off
            return True

        mock_k32.GetConsoleMode.side_effect = set_raw_mode

        assert helper.probe_at_prompt(100) is True
        mock_k32.CloseHandle.assert_called_once_with(conin_handle)


class TestHelperResponseChannelGuard:
    """fd-1 restore failure must escalate, not silently leave fd 1 foreign."""

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    def test_fd1_restore_failure_raises(self, mock_k32, mock_os):
        # dup saves fds 0/1/2; the restore of fd 1 raises OSError, all others
        # succeed. The helper must raise ResponseChannelCorrupted so run_loop
        # recycles rather than write the next response to a foreign fd 1.
        mock_os.dup.side_effect = lambda fd: 100 + fd  # saved fds 100/101/102
        mock_k32.AttachConsole.return_value = True

        def set_mode(handle, mode_ptr):
            mode_ptr._obj.value = 0x0200
            return True

        mock_k32.GetConsoleMode.side_effect = set_mode

        # Make ONLY the restore of fd 1 (os.dup2(saved, 1)) fail.
        def _dup2(saved_fd, fd_num):
            if fd_num == 1:
                raise OSError("restore of fd 1 failed")
            return 0

        mock_os.dup2.side_effect = _dup2

        with pytest.raises(helper.ResponseChannelCorrupted):
            helper._has_interactive_child(200)

    @patch("ui.console_probe_helper.os")
    @patch("ui.console_probe_helper._kernel32")
    def test_fd0_restore_failure_does_not_raise(self, mock_k32, mock_os):
        # A failed restore of fd 0 or fd 2 is NOT the response channel, so it
        # must NOT escalate -- it stays swallowed as before.
        mock_os.dup.side_effect = lambda fd: 100 + fd
        mock_k32.AttachConsole.return_value = True

        def set_mode(handle, mode_ptr):
            mode_ptr._obj.value = 0x0200
            return True

        mock_k32.GetConsoleMode.side_effect = set_mode

        def _dup2(saved_fd, fd_num):
            if fd_num == 0:
                raise OSError("restore of fd 0 failed")
            return 0

        mock_os.dup2.side_effect = _dup2

        # No raise: returns a normal bool result.
        result = helper._has_interactive_child(200)
        assert isinstance(result, bool)
