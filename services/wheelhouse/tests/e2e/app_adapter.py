"""Bridge between speech pipeline (send_command/send_request) and UIActionHandler.

Mirrors the dispatch logic from input_proc.py lines 544-572:
  action = command_message.get("action")
  params = command_message.get("params", {}) or {}
  method_to_call = getattr(ui_handler, action)
  method_to_call(**params)

The adapter owns OS-level patches so UIActionHandler can be constructed
without touching real Windows APIs.
"""
import logging
from multiprocessing import Queue
from typing import Dict, Any, Optional
from unittest.mock import MagicMock, patch

from services.wheelhouse.tests.e2e.os_mocks import Recording, make_mock_context

logger = logging.getLogger(__name__)


class AppAdapter:
    """Implements send_command/send_request interface, dispatches to UIActionHandler.

    This replaces MockApp in E2E tests. Instead of recording dicts, it
    actually calls UIActionHandler methods so the full dispatch + strategy
    logic executes.

    Owns OS-level patches: call stop_patches() when done.
    """

    def __init__(self, recording: Recording, config: Optional[dict] = None,
                 context_kwargs: Optional[dict] = None):
        self.recording = recording
        self._response_queue = Queue()
        self._config = config or {}
        self._patches = []

        # Apply OS-level patches BEFORE constructing UIActionHandler
        self._apply_patches(context_kwargs or {})

        from services.wheelhouse.ui.ui_action_handler import UIActionHandler

        self.handler = UIActionHandler(
            response_queue=self._response_queue,
            config=self._config,
        )

    def _apply_patches(self, context_kwargs):
        """Apply all OS-level patches needed by UIActionHandler and its deps."""
        ctx = make_mock_context(**context_kwargs)

        patches = [
            # context.py imports uiautomation and psutil at module level
            patch("services.wheelhouse.ui.context.auto", new=MagicMock()),
            patch("services.wheelhouse.ui.context.psutil", new=MagicMock()),
            patch("services.wheelhouse.ui.context.capture_context", return_value=ctx),

            # ui_action_handler.py capture_context mock
            patch("services.wheelhouse.ui.ui_action_handler.capture_context", return_value=ctx),

            # win_input_sender.press_keys -> recording
            patch("services.wheelhouse.ui.ui_action_handler.press_keys",
                  side_effect=self.recording.press_keys),
            patch("services.wheelhouse.ui.clipboard_operations.press_keys",
                  side_effect=self.recording.press_keys),

            # win_input_sender.type_string -> recording
            patch("services.wheelhouse.ui.ui_action_handler.type_string",
                  side_effect=self.recording.type_string),

            # strategies/specific.py imports uiautomation at module level
            patch("services.wheelhouse.ui.strategies.specific.auto", new=MagicMock()),

            # wh-wxkp: mock the UIA surface ShadowBufferManager.synchronize()
            # walks so VerifiedUnicodeStrategy's buffer-sync gate passes and
            # TextPerfector sees deterministic preceding context (empty
            # document, caret at end). The module is reachable under two
            # import paths (same dual-cache situation as ui.hwnd_utils
            # below), so patch both.
            patch("services.wheelhouse.ui.shadow_buffer.auto",
                  new=self._make_mock_shadow_buffer_auto()),
            patch("ui.shadow_buffer.auto",
                  new=self._make_mock_shadow_buffer_auto()),

            # wh-wxkp: Unicode delivery boundary. type_string_verified would
            # fire real Win32 SendInput; record instead (returns the
            # (success, chars_sent, error) triple).
            patch("services.wheelhouse.ui.strategies.specific.type_string_verified",
                  side_effect=self.recording.type_string_verified),

            # wh-wxkp: the dispatch log's modifier snapshot reads real
            # GetAsyncKeyState; freeze it so a developer holding Shift while
            # tests run cannot change the log path.
            patch("services.wheelhouse.ui.strategies.specific.snapshot_modifier_state",
                  return_value="mods=mocked"),

            # wh-wxkp: post-send foreground check inside
            # VerifiedUnicodeStrategy.insert. Same contract as the
            # clipboard_operations mock below: the mocked focused_control's
            # NativeWindowHandle resolves to 1, so foreground must be 1.
            patch("services.wheelhouse.ui.strategies.specific.win32gui",
                  new=self._make_mock_clipboard_win32gui()),

            # wh-wxkp: the same-process browser fallback resolver walks
            # win32process + psutil on the fake HWND; None means "not a
            # known browser", which keeps the strict GA_ROOT compare.
            patch("services.wheelhouse.ui.strategies.specific.process_name_for_hwnd",
                  return_value=None),

            # wh-wxkp: the wh-trailing-corruption-phase2 diagnostic readback
            # would walk UIA TextPattern on the MagicMock control; None is
            # the documented "readback unavailable" outcome and short-
            # circuits it deterministically.
            patch("services.wheelhouse.ui.strategies.specific.read_context_via_text_pattern",
                  return_value=None),

            # clipboard_operations uses pyperclip
            patch("services.wheelhouse.ui.clipboard_operations.pyperclip",
                  new=self._make_mock_pyperclip()),

            # clipboard_manager uses pyperclip for save/restore
            patch("services.wheelhouse.utils.clipboard_manager.pyperclip",
                  new=self._make_mock_pyperclip()),

            # utterance_clipboard_manager uses pyperclip directly
            patch("services.wheelhouse.ui.utterance_clipboard_manager.pyperclip",
                  new=self._make_mock_pyperclip()),

            # ui_action_handler imports clipboard_context
            patch("services.wheelhouse.ui.ui_action_handler.clipboard_context",
                  new=self._make_mock_clipboard_context()),

            # window_focus_manager uses win32gui and win32con
            patch("services.wheelhouse.ui.window_focus_manager.win32gui", new=MagicMock()),
            patch("services.wheelhouse.ui.window_focus_manager.win32con", new=MagicMock()),

            # wh-59i32: clipboard_operations.verified_paste calls
            # win32gui.GetForegroundWindow for the post-paste foreground check.
            # The mocked focused_control's NativeWindowHandle resolves to 1 via
            # MagicMock.__int__, so the foreground mock returns 1 too — without
            # this, the real GetForegroundWindow returns whatever has focus on
            # the host machine and the post-paste check rejects every paste.
            patch(
                "services.wheelhouse.ui.clipboard_operations.win32gui",
                new=self._make_mock_clipboard_win32gui(),
            ),

            # wh-oe7u.3: verified_paste, _hwnd_from_control, and retract
            # focus checks all go through normalize_hwnd_for_foreground_compare,
            # which calls win32gui.GetAncestor(hwnd, GA_ROOT) inside
            # ui.hwnd_utils. Real GetAncestor on the fake HWND 1 raises
            # "invalid window handle" and the helper returns None,
            # blocking every paste. The module is reachable under two
            # different import paths (``ui.hwnd_utils`` via the
            # wheelhouse service sys.path entry and
            # ``services.wheelhouse.ui.hwnd_utils`` via the project root
            # entry); both paths cache the module separately, so patch
            # both.
            patch(
                "services.wheelhouse.ui.hwnd_utils.win32gui",
                new=self._make_mock_hwnd_utils_win32gui(),
            ),
            patch(
                "ui.hwnd_utils.win32gui",
                new=self._make_mock_hwnd_utils_win32gui(),
            ),

            # subprocess.Popen so run_program() records instead of executing
            patch("services.wheelhouse.speech.actions.subprocess.Popen",
                  side_effect=lambda cmd, **kw: self.recording.run_programs.append(str(cmd))),

            # webbrowser.open so GSearch() records instead of opening a real browser
            patch("services.wheelhouse.speech.actions.webbrowser.open",
                  side_effect=lambda url, **kw: self.recording.run_programs.append(str(url))),

            # Patch pyperclip.copy/paste at the module level to catch late imports
            # (ui_action_handler.py and speech/actions.py do `import pyperclip` inside
            # method bodies, bypassing the clipboard_operations.pyperclip mock)
            patch("pyperclip.copy", side_effect=self._mock_pyperclip_copy),
            patch("pyperclip.paste", side_effect=self._mock_pyperclip_paste),
        ]

        for p in patches:
            p.start()
            self._patches.append(p)

    def _mock_pyperclip_copy(self, text):
        """Mock pyperclip.copy -- shared by module-level mock and direct patches."""
        self.recording.clipboard_state = text
        # Don't record internal sentinel values -- only real pastes
        if not (isinstance(text, str) and "__SENTINEL_" in text):
            self.recording.clipboard_pastes.append(text)

    def _mock_pyperclip_paste(self):
        """Mock pyperclip.paste -- shared by module-level mock and direct patches."""
        return self.recording.clipboard_state

    def _make_mock_pyperclip(self):
        """Create a mock pyperclip that uses recording's clipboard_state.

        Sentinel values (used by gather_context for clipboard round-trips)
        are filtered out of clipboard_pastes so tests only see real insertions.
        """
        mock = MagicMock()
        mock.paste.side_effect = self._mock_pyperclip_paste
        mock.copy.side_effect = self._mock_pyperclip_copy
        return mock

    def _make_mock_clipboard_context(self):
        """Create a mock clipboard_context context manager."""
        from contextlib import contextmanager

        @contextmanager
        def mock_ctx(*args, **kwargs):
            yield

        return mock_ctx

    def _make_mock_hwnd_utils_win32gui(self):
        """Mock win32gui inside ui.hwnd_utils so GetAncestor returns identity.

        The fake test HWNDs (typically 1, the MagicMock __int__ default)
        are not real Win32 windows; real GetAncestor would raise. With
        identity-stub GetAncestor, normalize_hwnd_for_foreground_compare
        returns the same HWND for both expected and observed sides, so
        the post-paste comparison and retract focus check pass for the
        e2e test scenarios (wh-oe7u.3).
        """
        mock = MagicMock()
        # GetAncestor(hwnd, GA_ROOT) -> hwnd. Both expected and observed
        # sides see the same value, so equality holds for any non-zero
        # HWND the test passes through.
        mock.GetAncestor.side_effect = lambda hwnd, _flag: hwnd
        return mock

    def _make_mock_shadow_buffer_auto(self, text: str = ""):
        """Fake uiautomation module for ShadowBufferManager.synchronize().

        wh-wxkp: shapes the mock so the REAL synchronize() code path runs
        and produces a usable buffer: full document text ``text``, no
        selection, caret at end of document.

        The fast-path _get_cursor_pos_fast bails on its own: the raw
        GetCaretRange() MagicMock fails tuple unpacking (ValueError, in
        the method's except list), so synchronize() falls back to the
        MoveEndpointByRange path, where doc_range.Clone().GetText(-1)
        returning the full text puts cursor_pos at len(text).
        """
        auto = MagicMock()
        cursor_range = MagicMock()
        cursor_range.GetText.return_value = text  # caret at end of document
        doc_range = MagicMock()
        doc_range.GetText.return_value = text
        doc_range.Clone.return_value = cursor_range
        sel_range = MagicMock()
        sel_range.GetText.return_value = ""  # no selection
        text_pattern = MagicMock()
        text_pattern.DocumentRange = doc_range
        text_pattern.GetSelection.return_value = [sel_range]
        focused = MagicMock()
        focused.GetPattern.return_value = text_pattern
        auto.GetFocusedControl.return_value = focused
        return auto

    def _make_mock_clipboard_win32gui(self):
        """Mock win32gui for clipboard_operations.verified_paste's post-paste check.

        The mocked focused_control's NativeWindowHandle resolves to 1 (MagicMock's
        default __int__). The strategy passes that as target_hwnd, so the
        post-paste check expects 1. Returning 1 from GetForegroundWindow keeps
        the check satisfied without forcing every test to set up real HWNDs.
        """
        mock = MagicMock()
        mock.GetForegroundWindow.return_value = 1
        return mock

    def stop_patches(self):
        """Stop all OS-level patches."""
        for p in self._patches:
            p.stop()
        self._patches.clear()

    async def send_command(self, payload: Dict[str, Any]) -> None:
        """Dispatch action dict to UIActionHandler, mirroring input_proc.py."""
        action = payload.get("action", "")
        params = payload.get("params", {}) or {}

        if not hasattr(self.handler, action):
            logger.warning("AppAdapter: unknown action '%s'", action)
            return

        method = getattr(self.handler, action)
        method(**params)

    async def send_request(self, action: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Dispatch and return success (no real IPC round-trip needed)."""
        payload = {"action": action, "params": params or {}}
        await self.send_command(payload)
        return True
