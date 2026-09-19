"""Bridge between speech pipeline (send_command/send_request) and UIActionHandler.

Mirrors the dispatch logic from input_proc.py lines 544-572:
  action = command_message.get("action")
  params = command_message.get("params", {}) or {}
  method_to_call = getattr(ui_handler, action)
  method_to_call(**params)

The adapter owns OS-level patches so UIActionHandler can be constructed
without touching real Windows APIs.
"""
import asyncio
import logging
# ThreadQueue is the synchronous in-process queue for the special-route
# acknowledgements (wh-overlay-slow-uia-stale-badges.14.31): the
# multiprocessing Queue below hands put() to a feeder thread, so an
# immediate get_nowait() on it races.
from queue import Empty, Queue as ThreadQueue
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

    # The single window the end-to-end tests target. The mocked
    # focused_control's NativeWindowHandle resolves to 1 through
    # MagicMock.__int__, and both win32gui stand-ins below report this
    # HWND as the foreground window so the focus checks in
    # window_focus_manager, clipboard_operations, and
    # strategies/specific describe the same window
    # (wh-review-pattern-fixes.41).
    FOREGROUND_HWND = 1

    def __init__(self, recording: Recording, config: Optional[dict] = None,
                 context_kwargs: Optional[dict] = None,
                 foreground_hwnd: Optional[int] = None,
                 action_handler_foreground: bool = False):
        """Build a UIActionHandler with every OS boundary mocked.

        ``foreground_hwnd`` overrides the window handle the win32gui
        stand-ins report as the foreground window. It defaults to
        ``FOREGROUND_HWND``, the handle the mocked focused control
        resolves to, which is what every focus proof needs to pass. A test
        passes a different value to prove that a run whose focus proof
        refuses delivers no text (wh-review-pattern-fixes.46).

        ``action_handler_foreground`` extends that stand-in to
        ``ui_action_handler``'s OWN win32gui, whose
        ``GetForegroundWindow`` is otherwise the real one. It is
        opt-in, and default False, because turning it on changes two
        other seams in that module -- the retract focus gate at
        ui_action_handler.py:1280 and the insert focus proof at
        :1645 -- and every existing e2e test was written against
        their real-foreground answers. A test that needs the retract
        path to run at all passes True
        (wh-spaced-punctuation-names-unresolved.3.1.2).
        """
        self.recording = recording
        self._response_queue = Queue()
        self._config = config or {}
        self._patches = []
        self.foreground_hwnd = (
            self.FOREGROUND_HWND if foreground_hwnd is None else foreground_hwnd
        )
        self._action_handler_foreground = action_handler_foreground
        # wh-spaced-punctuation-names-unresolved.3.1.5: the same
        # running count of start_utterance commands the production app
        # keeps (app.py:1132). The processor reads it immediately
        # before each delivery, and ``E2EPipelineHarness.send_word``
        # copies it onto the first word of an utterance, so a test can
        # measure whether its text went out before or after the
        # command that resets the paste counter.
        self.utterance_start_generation = 0

        # Apply OS-level patches BEFORE constructing UIActionHandler
        self._apply_patches(context_kwargs or {})

        from services.wheelhouse.ui.ui_action_handler import UIActionHandler

        self.handler = UIActionHandler(
            response_queue=self._response_queue,
            config=self._config,
        )

        self._inject_click_seams()

    def _inject_click_seams(self):
        """Give the click feature a fake element tree and fake press providers.

        wh-review-pattern-fixes.47. Three things cannot be reached by a
        module patch, so they are injected onto the built objects:

        1. ``handler._click_automation_root``. Setting it makes
           ``_get_click_element_finder`` skip ``create_automation`` and
           gives ``_point_hits_winner_via_automation_root`` a usable root
           (it raises when the root is missing).
        2. ``handler._click_element_finder``. The handler honours an
           already-set finder and returns it before it builds one, so the
           by-name and numbered-badge paths both walk the fake snapshot.
        3. The executor's two press providers. ``ClickExecutor`` binds
           ``invoke_via_invoke_pattern`` and
           ``do_default_action_via_legacy_pattern`` as constructor DEFAULT
           arguments, so the binding is made when the class body runs. No
           module-level patch can replace a default argument; the
           executor is built here and its attributes are replaced with
           recorders.

        Building the executor here also memoises it, so every later
        ``_get_click_executor`` call in a dispatch returns this one.
        """
        from services.wheelhouse.tests.e2e.os_mocks import (
            FakeAutomationRoot, FakeClickFinder,
        )

        # The click stand-ins report the window the mocked focused control
        # resolves to. This is deliberately NOT the wh-review-pattern-fixes.46
        # foreground_hwnd override: that override exists to make the
        # DICTATION focus proofs refuse, and a click test must stay
        # deterministic either way.
        self.recording.click_foreground_window = self.FOREGROUND_HWND

        self.handler._click_automation_root = FakeAutomationRoot()
        self.handler._click_element_finder = FakeClickFinder(self.recording)

        executor = self.handler._get_click_executor()
        executor._invoke_fn = self.recording.invoke_element
        executor._do_default_action_fn = (
            self.recording.do_default_action_on_element
        )

    def _apply_patches(self, context_kwargs):
        """Apply all OS-level patches needed by UIActionHandler and its deps."""
        ctx = make_mock_context(**context_kwargs)

        from services.wheelhouse.ui.clipboard_operations import (
            ClipboardOperations,
        )

        # wh-review-pattern-fixes.42: keep the hit-test answer and the
        # foreground answer describing the same window. Recording carries
        # its own default so it stays usable without an adapter; the
        # adapter owns the value while its patches are live.
        self.recording.hit_test_hwnd = self.FOREGROUND_HWND

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

            # wh-review-pattern-fixes.36: clipboard_operations no longer
            # imports press_keys. Every non-Flutter state-changing chord
            # goes through verified_press_keys, so the recorder binds
            # there instead; the failure-recovery helper is stubbed
            # because a recorded chord never reports a short delivery.
            patch("services.wheelhouse.ui.clipboard_operations.verified_press_keys",
                  side_effect=self.recording.verified_press_keys),
            patch("services.wheelhouse.ui.clipboard_operations._send_modifier_keyups",
                  new=MagicMock()),

            # wh-review-pattern-fixes.40: ui_action_handler binds three
            # more win_input_sender symbols at import time, and none of
            # them went through the press_keys mock above.
            # verified_press_keys carries the Ctrl+C, Ctrl+A and Ctrl+V
            # chords of transform_selection, wrap_or_insert,
            # capture_selected_text, replace_selected_text and
            # press_key_verified; send_backspaces carries retract's
            # Backspace batch. Unpatched, each one reached real Win32
            # SendInput and hit whichever window held focus on the
            # developer machine. _send_modifier_keyups is the
            # short-delivery recovery helper; a recorded chord always
            # reports full delivery, so it never runs, and the stub keeps
            # a future failure-path test off the real boundary too.
            patch("services.wheelhouse.ui.ui_action_handler.verified_press_keys",
                  side_effect=self.recording.verified_press_keys),
            patch("services.wheelhouse.ui.ui_action_handler._send_modifier_keyups",
                  new=MagicMock()),
            patch("services.wheelhouse.ui.ui_action_handler.send_backspaces",
                  side_effect=self.recording.send_backspaces),

            # win_input_sender.type_string -> recording
            patch("services.wheelhouse.ui.ui_action_handler.type_string",
                  side_effect=self.recording.type_string),
            # 43c2b931 (wh-keyboard-refusal-notice): type_text now calls
            # type_string_verified through this module. Left unpatched it
            # fires real Win32 SendInput into whatever window has focus.
            patch("services.wheelhouse.ui.ui_action_handler.type_string_verified",
                  side_effect=self.recording.type_text_verified),
            *(
                [patch(
                    "services.wheelhouse.ui.ui_action_handler.win32gui",
                    new=self._make_mock_action_handler_win32gui(),
                )]
                if self._action_handler_foreground else []
            ),

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
            # (.1.34: the resolver now returns a (pid, name) pair.)
            patch("services.wheelhouse.ui.strategies.specific.process_identity_for_hwnd",
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
            patch("services.wheelhouse.ui.window_focus_manager.win32gui",
                  new=self._make_mock_focus_win32gui()),
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

            # wh-review-pattern-fixes.42: the mouse boundary. Six wrappers
            # in ui_action_handler import their provider from
            # utils.win_input_sender INSIDE the function body, so the
            # patches above -- which all name the ui_action_handler module
            # -- cannot intercept them. The patch must therefore name the
            # provider module. UIActionHandler stores _win32_click_point,
            # _win32_move_pointer and _win32_drag_pointer in
            # _mouse_click_seam, _mouse_move_seam and _mouse_drag_seam, and
            # click_point, move_pointer and perform_drag call them; the
            # by-name and snapshot click paths give ClickExecutor
            # _win32_coordinate_click, _win32_gesture_click and
            # _win32_root_window_at_point. The adapter's empty config
            # reaches ClickConfig's enabled=True default, so none of those
            # dispatches short-circuits. Unpatched, the first e2e test that
            # dispatches a mouse action moved, clicked or dragged the
            # developer's real mouse while passing.
            #
            # Every call site names utils.win_input_sender, so this one
            # module path covers all six wrappers. That differs from
            # ui.hwnd_utils above, where production reaches the same module
            # under two import paths.
            patch("utils.win_input_sender.click_point",
                  side_effect=self.recording.click_point),
            patch("utils.win_input_sender.move_pointer_to",
                  side_effect=self.recording.move_pointer_to),
            patch("utils.win_input_sender.drag_pointer",
                  side_effect=self.recording.drag_pointer),
            patch("utils.win_input_sender.click_at",
                  side_effect=self.recording.click_at),

            # wh-review-pattern-fixes.42: root_window_at_point is a host
            # read, not an input send, but it belongs with the click seams.
            # ClickExecutor refuses a coordinate click whose point resolves
            # to a window other than the target, so a real read would make
            # the refusal depend on which window sat under the point on the
            # developer's screen. The recorder answers FOREGROUND_HWND --
            # the one window these tests target -- for every point.
            patch("utils.win_input_sender.root_window_at_point",
                  side_effect=self.recording.root_window_at_point),

            # wh-review-pattern-fixes.47: the voice-click boundary.
            #
            # Module path matters here, and the two halves differ.
            # ``_capture_click_foreground``, ``_win32_foreground_probe``,
            # ``_win32_on_screen`` and ``_uia_point_hits_winner`` are
            # module globals of ui_action_handler, which the adapter
            # imports as ``services.wheelhouse.ui.ui_action_handler``, so
            # that is the path the patches name. The walker symbols are
            # read through ``from ui import uia_walker`` INSIDE
            # ``_get_click_element_finder`` and ``_get_click_executor``,
            # so a patch aimed at ``services.wheelhouse.ui.uia_walker``
            # would leave the real functions reachable -- the module is
            # cached separately under each import path.
            #
            # ``_get_click_executor`` reads the four handler globals when
            # it BUILDS the executor, so these patches must be live before
            # the build. They are: the build happens in __init__ after
            # _apply_patches returns.
            patch(
                "services.wheelhouse.ui.ui_action_handler."
                "_capture_click_foreground",
                side_effect=self.recording.capture_click_foreground,
            ),
            patch(
                "services.wheelhouse.ui.ui_action_handler."
                "_win32_foreground_probe",
                side_effect=self.recording.click_foreground_probe,
            ),
            patch(
                "services.wheelhouse.ui.ui_action_handler._win32_on_screen",
                side_effect=self.recording.click_on_screen,
            ),
            patch(
                "services.wheelhouse.ui.ui_action_handler."
                "_uia_point_hits_winner",
                side_effect=self.recording.uia_point_hits_winner,
            ),
            # The popup-liveness, popup-owner and shell-class seams the
            # executor holds. Pure Win32 reads of real windows.
            patch("ui.uia_walker._default_is_window_visible",
                  side_effect=self.recording.popup_is_visible),
            patch("ui.uia_walker._default_owner_of",
                  side_effect=self.recording.popup_owner_of),
            patch("ui.uia_walker._default_class_name_of",
                  side_effect=self.recording.shell_class_of),
            # The COM root builder. The adapter injects the root below, so
            # nothing should reach this; the recorder makes a path that
            # does reach it visible instead of letting it build COM state.
            patch("ui.uia_walker.create_automation",
                  side_effect=self.recording.create_automation),
            # The deeper half of the UIA obstruction check.
            # ``_uia_point_hits_winner`` above already stands between the
            # executor and this function, so this patch is the backstop
            # for any other caller. Both recorders write the same list.
            patch("ui.uia_walker.point_hits_winner",
                  side_effect=self.recording.uia_point_hits_winner),

            # wh-review-pattern-fixes.46: the delivery record. See
            # _record_text_delivery and _record_selection_restore below
            # for why these two methods are the only hooks.
            patch.object(
                ClipboardOperations, "credit_paste_chars",
                new=self._make_delivery_recorder(
                    ClipboardOperations.credit_paste_chars,
                ),
            ),
            patch.object(
                ClipboardOperations, "_raw_paste",
                new=self._make_selection_restore_recorder(
                    ClipboardOperations._raw_paste,
                ),
            ),

            # wh-review-pattern-fixes.42: the notification boundary.
            # UIActionHandler.show_notification does `from plyer import
            # notification` inside the method and calls notification.notify,
            # which puts a real toast on the developer's screen.
            # plyer.notification is a lazy proxy: reading any attribute
            # imports the platform implementation, and both reads and
            # writes forward to that instance, so mock.patch reaches the
            # object the late import returns.
            patch("plyer.notification.notify",
                  side_effect=self.recording.notify),
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

    def _make_delivery_recorder(self, original):
        """Wrap credit_paste_chars so a credited text joins the record.

        wh-review-pattern-fixes.46: production calls
        ``ClipboardOperations.credit_paste_chars`` from exactly two places,
        and both of them run only after a delivery succeeded:

        1. ``ClipboardOperations.verified_paste``, on the last statement
           before ``return True``. Every earlier refusal -- a failed copy,
           a failed pre-send focus proof, a short Ctrl+V chord, a failed
           post-paste foreground check -- returns False before this line.
        2. ``VerifiedUnicodeStrategy.insert``, after
           ``type_string_verified`` reported a full send AND the post-send
           foreground check passed. Every failure path before it returns
           an unsuccessful InsertionResult.

        The wrapper calls the real method first, so the retract counters
        and the sticky Qt flag keep their production values, then records
        the text. Recording after the call also means a raising
        credit_paste_chars records nothing.

        ``_raw_paste`` is the one other place that sends a Ctrl+V and can
        report success; it deliberately does NOT credit, and
        ``_make_selection_restore_recorder`` covers it separately.
        """
        recording = self.recording

        def credit_paste_chars(clipboard_self, text, target_class_name=""):
            original(
                clipboard_self, text, target_class_name=target_class_name,
            )
            recording.text_deliveries.append(text)

        return credit_paste_chars

    def _make_selection_restore_recorder(self, original):
        """Wrap _raw_paste so a restored selection joins its own record.

        wh-review-pattern-fixes.46: ``_raw_paste`` is the selection-restore
        path. It sends a real Ctrl+V and returns True only when the copy
        succeeded, the captured target held the foreground, and SendInput
        accepted the whole chord. That is a genuine delivery, so it must
        not go unrecorded -- but the text is the user's own prior content
        rather than new dictation, and production keeps it out of every
        retract-accounting field, so it gets ``selection_restores`` rather
        than ``text_deliveries``.
        """
        recording = self.recording

        def _raw_paste(clipboard_self, text, window_manager, **kwargs):
            restored = original(
                clipboard_self, text, window_manager, **kwargs,
            )
            if restored:
                recording.selection_restores.append(text)
            return restored

        return _raw_paste

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
        post-paste check expects 1. Returning that same HWND from
        GetForegroundWindow keeps the check satisfied without forcing every
        test to set up real HWNDs.
        """
        mock = MagicMock()
        mock.GetForegroundWindow.return_value = self.foreground_hwnd
        return mock

    def _make_mock_action_handler_win32gui(self):
        """Report the test window as foreground inside ui_action_handler.

        wh-spaced-punctuation-names-unresolved.3.1.2: ``retract`` reads
        ``win32gui.GetForegroundWindow()`` directly
        (ui_action_handler.py:1280) and compares it with the remembered
        paste target. That module's win32gui is the real one, so the
        comparison saw whatever window held focus on the machine
        running the tests and every e2e retract answered
        ``focus_drifted`` before reaching a single backspace.

        The stand-in WRAPS the real module rather than replacing it, so
        only ``GetForegroundWindow`` changes: ``GetCursorPos`` and
        ``GetWindowRect`` keep the real behaviour the module's other
        callers were written against, including raising on the fake
        HWND 1.
        """
        import win32gui as real_win32gui

        mock = MagicMock(wraps=real_win32gui)
        mock.GetForegroundWindow.return_value = self.foreground_hwnd
        return mock

    def _make_mock_focus_win32gui(self):
        """Mock win32gui inside window_focus_manager with one foreground window.

        wh-review-pattern-fixes.41: verified_paste, _raw_paste, and
        VerifiedUnicodeStrategy now refuse to send when
        WindowFocusManager.ensure_focused reports False.
        ``ensure_focused`` reads win32gui.GetForegroundWindow() and
        compares it with the HWND it was asked to focus. A bare MagicMock
        returns a MagicMock from GetForegroundWindow, which never equals
        an integer HWND, so ensure_focused returned False inside every
        end-to-end test and the new pre-send proof aborted the paste.

        The stand-in reports FOREGROUND_HWND -- the value the mocked
        focused_control resolves to, which is the window these tests
        actually target -- as the foreground window. SetForegroundWindow
        stays a no-op, so the stand-in never claims that activating some
        other window succeeded: ensure_focused(FOREGROUND_HWND) returns
        True and ensure_focused of any other HWND returns False. A future
        test that targets a window which is not foreground still fails.
        """
        mock = MagicMock()
        mock.IsIconic.return_value = False
        mock.GetForegroundWindow.return_value = self.foreground_hwnd
        return mock

    def stop_patches(self):
        """Stop all OS-level patches."""
        for p in self._patches:
            p.stop()
        self._patches.clear()

    async def send_command(self, payload: Dict[str, Any]) -> None:
        """Dispatch action dict to UIActionHandler, mirroring input_proc.py.

        wh-overlay-slow-uia-stale-badges.14.25: mirrors the production
        boundary -- only an ABSENT params key defaults to {}; a supplied
        non-mapping is rejected without dispatch, like input_proc's
        _extract_params.

        wh-overlay-slow-uia-stale-badges.14.41: the whole payload runs
        through production's _read_envelope chokepoint first -- the
        reader drops a dict-subclass envelope and contains a poisoned
        dict key whose __eq__ raises inside .get, so the adapter must
        too, or an E2E workflow accepts an envelope production rejects.
        """
        import input_proc
        extracted = input_proc._read_envelope(payload)
        if extracted is None:
            logger.warning(
                "AppAdapter: dropping malformed command envelope of type %s",
                # .14.61: every type name here goes through production's
                # safe helper, or the adapter reports a different failure
                # than the real Input process for a hostile metaclass.
                input_proc._safe_type_name(payload),
            )
            return
        action, raw_params, has_params, _request_id, _trace_id = extracted
        # wh-overlay-slow-uia-stale-badges.14.37 + .14.38: mirror
        # input_proc's _validate_action -- a non-string (or
        # subclassed-string) fire-and-forget action is warning-dropped
        # before dispatch; the adapter's own hasattr raises for a str
        # subclass with a broken hash. Render the type name only.
        if type(action) is not str:
            logger.warning(
                "AppAdapter: rejecting non-string action of type %s",
                input_proc._safe_type_name(action),  # .14.61
            )
            return
        params = raw_params if has_params else {}
        # .14.38: mirror _coerce_params -- only an EXACT dict passes.
        if type(params) is not dict:
            logger.warning(
                "AppAdapter: rejecting non-mapping params for action '%s': "
                "type %s", action, input_proc._safe_type_name(params),  # .14.61
            )
            return

        if not hasattr(self.handler, action):
            logger.warning("AppAdapter: unknown action '%s'", action)
            return

        # .3.1.5: production bumps the count immediately before the
        # enqueue, once every validation gate above has passed
        # (app.py:1125-1133). Here the dispatch below IS the delivery,
        # so the same position is immediately before it.
        if action == 'start_utterance':
            self.utterance_start_generation += 1

        method = getattr(self.handler, action)
        method(**params)

    async def send_request(self, action: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Dispatch and return success (no real IPC round-trip needed).

        wh-overlay-slow-uia-stale-badges.14.26: a request with rejected
        params fails like production -- input_proc puts the standard
        error response and WheelHouseApp's demux raises RuntimeError to
        the send_request caller. Resolving True here would let an E2E
        workflow accept a malformed awaited action production rejects.
        """
        # wh-overlay-slow-uia-stale-badges.14.37 + .14.38: mirror
        # input_proc's _validate_action -- a non-string (or
        # subclassed-string) request action gets the standard error
        # response in production, which the demux raises as
        # RuntimeError; the adapter's own special-route comparisons and
        # hasattr raise for a str subclass with a raising __eq__ or a
        # broken hash. Render the type name only.
        # .14.61: both rejects name the value by its type, so both read
        # it through production's safe helper.
        import input_proc
        if type(action) is not str:
            raise RuntimeError(
                f"Invalid action: expected a string, "
                f"got {input_proc._safe_type_name(action)}"
            )
        resolved = params if params is not None else {}
        # .14.38: mirror _coerce_params -- only an EXACT dict passes.
        if type(resolved) is not dict:
            raise RuntimeError(
                f"Invalid params for action '{action}': "
                f"expected a mapping, got {input_proc._safe_type_name(resolved)}"
            )
        # wh-overlay-slow-uia-stale-badges.14.29: input_proc answers
        # these routes BEFORE its generic hasattr branch, so an awaited
        # send of them resolves in production even though none is a
        # UIActionHandler method. _te_event_ack and add_soft_allow_tuple
        # run through input_proc's own handlers against a local queue,
        # and an error response becomes RuntimeError exactly as the
        # demux raises it. activate_window is acknowledged after win32
        # work production performs; the OS layer is mocked in E2E, so
        # it resolves without dispatch.
        if action == "activate_window":
            return True
        # .14.60: terminal_editor_cancelled is the third acknowledged
        # special route. Production dispatches it through its own
        # handler, which consumes only params["request_id"]; the
        # fall-through called UIActionHandler.terminal_editor_cancelled
        # with **params, so an extra key raised TypeError in E2E for a
        # command production accepts and answers.
        if action in (
            "_te_event_ack", "add_soft_allow_tuple", "terminal_editor_cancelled",
        ):
            import input_proc
            handler_fn = {
                "_te_event_ack": input_proc._handle_te_event_ack_command,
                "add_soft_allow_tuple": input_proc._handle_add_soft_allow_tuple,
                "terminal_editor_cancelled":
                    input_proc._handle_terminal_editor_cancelled,
            }[action]
            response_queue = ThreadQueue()
            handler_fn(resolved, "e2e-request", response_queue, self.handler)
            response = response_queue.get_nowait()
            if response.get("error"):
                raise RuntimeError(response.get("message", "UI process error"))
            return True
        # wh-spaced-punctuation-names-unresolved.3.1.2: retract is the
        # fourth special route. input_proc.py:2032-2053 answers it
        # directly so the RESPONSE carries the handler's own
        # {"status", "reason", "chars"} dict rather than the generic
        # ok/error shape, and SpeechProcessor._handle_retraction reads
        # response.get("status") from it. Falling through to the
        # dispatch branch below returned True, so that read raised
        # AttributeError and the whole retraction handler died before
        # the replay -- no e2e test could reach the retract path at
        # all. Production wraps the call and turns a raise into
        # status=not_retracted; mirror that too.
        if action == "retract":
            try:
                return self.handler.retract()
            except Exception as e:
                logger.error("Error in retract: %s", e, exc_info=True)
                return {
                    "status": "not_retracted",
                    "reason": f"error: {e}",
                    "action": action,
                }
        # .14.55: set_log_level became CONDITIONALLY acknowledged in
        # .14.40 -- a valid level keeps the route's historical silence,
        # and a malformed one gets the standard error response, which
        # the demux raises as RuntimeError. Without this route the
        # adapter fell through to the hasattr branch below and reported
        # a definitive Input-side rejection as an unanswered request.
        if action == "set_log_level":
            import input_proc
            response_queue = ThreadQueue()
            input_proc._handle_set_log_level(
                resolved, "e2e-request", response_queue,
            )
            try:
                response = response_queue.get_nowait()
            except Empty:
                raise asyncio.TimeoutError(
                    "Request 'set_log_level' is never answered on the "
                    "valid path: the route acknowledges nothing"
                ) from None
            if response.get("error"):
                raise RuntimeError(response.get("message", "UI process error"))
            return True
        # wh-overlay-slow-uia-stale-badges.14.27: production never
        # answers an action the input process has no handler method for
        # (it warns and continues without putting a response), so the
        # real send_request raises asyncio.TimeoutError. Resolving True
        # here would let a misspelled awaited action pass in E2E.
        if not hasattr(self.handler, action):
            logger.warning("AppAdapter: unknown request action '%s'", action)
            raise asyncio.TimeoutError(
                f"Request '{action}' would never be answered: "
                "the UI handler has no such method"
            )
        await self.send_command({"action": action, "params": resolved})
        return True
