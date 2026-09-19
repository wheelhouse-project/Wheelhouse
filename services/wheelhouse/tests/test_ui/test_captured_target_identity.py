"""Original-context identity must survive strategy delays without rebinding.

Real capture, composition, strategy wrappers, and clipboard delivery run here.
Only UIA/Win32/clipboard boundary calls use the deterministic desktop fixture;
no test touches the real foreground, clipboard, or input stream.
"""
from types import SimpleNamespace

import pytest

from ui import context as context_module
from ui import hwnd_utils
from ui import target_identity as target_identity_module
from ui import clipboard_operations as clipboard_module
from ui.shadow_buffer import ShadowBufferManager
from ui.text_perfector import TextPerfector
from ui.strategies import specific


@pytest.fixture
def desktop(monkeypatch):
    state = SimpleNamespace(
        windows={41: (100, 7), 100: (100, 7), 200: (200, 8)},
        markers={}, foreground=100, stale=False, clipboard="original",
        sends=[], after_copy=lambda: None, before_unicode=lambda: None,
        top_class="Window",
    )

    class Control:
        ClassName = "Edit"
        ProcessId = 7

        def GetTopLevelControl(self):
            if state.stale:
                return None
            return SimpleNamespace(NativeWindowHandle=41, ClassName=state.top_class)

        def SetFocus(self):
            pass

        def Exists(self, *args):
            return True

        def SendKeys(self, keys):
            state.sends.append(("flutter", state.foreground, state.clipboard))

    state.control = Control()
    monkeypatch.setattr(context_module.auto, "GetFocusedControl", lambda: state.control)
    monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "notepad.exe"))
    monkeypatch.setattr(hwnd_utils.win32gui, "GetAncestor", lambda hwnd, flag: state.windows.get(hwnd, (0, 0))[0])
    monkeypatch.setattr(hwnd_utils.win32gui, "IsWindow", lambda hwnd: hwnd in state.windows)
    monkeypatch.setattr(hwnd_utils.win32gui, "GetForegroundWindow", lambda: state.foreground)
    monkeypatch.setattr(hwnd_utils.win32process, "GetWindowThreadProcessId", lambda hwnd: (1, state.windows.get(hwnd, (0, 0))[1]))
    monkeypatch.setattr(hwnd_utils._user32, "GetPropW", lambda hwnd, key: state.markers.get(hwnd, 0))

    def set_prop(hwnd, key, value):
        if hwnd not in state.windows:
            return False
        state.markers[hwnd] = value
        return True

    monkeypatch.setattr(hwnd_utils._user32, "SetPropW", set_prop)

    def copy(text):
        state.clipboard = text
        state.after_copy()

    monkeypatch.setattr(clipboard_module.pyperclip, "copy", copy)
    monkeypatch.setattr(clipboard_module.pyperclip, "paste", lambda: state.clipboard)
    monkeypatch.setattr(clipboard_module, "get_sequence_number", lambda: 3)
    monkeypatch.setattr(clipboard_module, "wait_for_clipboard_write", lambda *args, **kwargs: False)
    monkeypatch.setattr(clipboard_module.time, "sleep", lambda _: None)

    def paste(*keys):
        state.sends.append(("paste", state.foreground, state.clipboard))
        return True, 4, 4

    monkeypatch.setattr(clipboard_module, "verified_press_keys", paste)
    monkeypatch.setattr(clipboard_module, "_send_modifier_keyups", lambda _: None)
    monkeypatch.setattr(specific, "snapshot_modifier_state", lambda: state.before_unicode())

    def unicode(text):
        state.sends.append(("unicode", state.foreground, text))
        return True, len(text), None

    monkeypatch.setattr(specific, "type_string_verified", unicode)
    state.window_manager = SimpleNamespace(ensure_focused=lambda hwnd: True)
    state.ops = clipboard_module.ClipboardOperations({})
    state.buffer = ShadowBufferManager()
    state.buffer._buffer = ""
    state.buffer._cursor_pos = 0
    state.buffer._selection_len = 0
    state.perfector = TextPerfector()
    return state


def strategy_pair(desktop):
    args = desktop.buffer, desktop.perfector, desktop.ops, desktop.window_manager
    return specific.VerifiedUnicodeStrategy(*args), specific.StandardStrategy(*args)


def test_fallback_uses_identity_captured_before_the_control_turns_stale(desktop):
    context = context_module.capture_context()
    unicode, standard = strategy_pair(desktop)

    def first_attempt(hwnd):
        desktop.stale = True
        return False

    unicode.window_manager = SimpleNamespace(ensure_focused=first_attempt)
    result = specific.UnicodeFirstStrategy(unicode, standard, desktop.ops).insert("hello", context)
    assert result.success
    assert desktop.sends == [("paste", 100, "Hello")]


@pytest.mark.parametrize("change", ["destroy", "reuse_same_pid", "reuse_other_pid", "reparent"])
def test_window_identity_change_during_clipboard_verification_refuses_input(desktop, change):
    context = context_module.capture_context()

    def change_window():
        if change == "destroy":
            desktop.windows.pop(41)
        elif change == "reuse_same_pid":
            desktop.markers.pop(41, None)
        elif change == "reuse_other_pid":
            desktop.windows[41] = (100, 8)
        else:
            desktop.windows[41] = (200, 7)

    desktop.after_copy = change_window
    _, standard = strategy_pair(desktop)
    result = standard.shadow_strategy.insert("hello", context)
    assert not result.success
    assert desktop.sends == []
    assert desktop.ops.last_paste_was_sent is False
    assert desktop.buffer._buffer == ""


def test_unicode_rechecks_focus_after_modifier_snapshot_before_send(desktop):
    context = context_module.capture_context()
    desktop.before_unicode = lambda: setattr(desktop, "foreground", 200)
    unicode, _ = strategy_pair(desktop)
    result = unicode.insert("hello", context)
    assert not result.success
    assert desktop.sends == []
    assert desktop.ops.last_paste_was_sent is False


def test_context_captures_raw_root_pid_and_window_object_provenance(desktop):
    context = context_module.capture_context()
    identity = getattr(context, "target_identity", None)
    assert identity is not None
    assert (identity.hwnd, identity.root, identity.process_id) == (41, 100, 7)
    assert identity.tag == desktop.markers[41] != 0
    assert identity.root_tag == desktop.markers[100] != 0


def test_capture_failure_is_explicit_and_never_falls_back_to_current_foreground(desktop):
    desktop.stale = True
    context = context_module.capture_context()
    desktop.stale = False
    _, standard = strategy_pair(desktop)
    result = standard.shadow_strategy.insert("hello", context)
    assert not result.success
    assert desktop.sends == []


@pytest.mark.parametrize("distinct_foreground", [False, True])
def test_recycled_root_with_same_raw_handle_and_pid_is_refused(desktop, monkeypatch, distinct_foreground):
    if distinct_foreground:
        # The root and foreground guards must each work independently.
        desktop.windows[300] = (300, 7)
        desktop.foreground = 300
        monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "brave.exe"))
        monkeypatch.setattr(hwnd_utils.win32gui, "IsWindowVisible", lambda hwnd: hwnd != 100)
        monkeypatch.setattr(hwnd_utils.win32gui, "GetWindowLong", lambda *args: 0)
    context = context_module.capture_context()
    desktop.after_copy = lambda: desktop.markers.pop(100, None)
    _, standard = strategy_pair(desktop)
    result = standard.shadow_strategy.insert("hello", context)
    assert not result.success
    assert desktop.sends == []


@pytest.mark.parametrize("copy_number", [1, 2, 3])
def test_slow_preparation_never_sends_keys_after_a_foreground_switch(desktop, monkeypatch, copy_number):
    context = context_module.capture_context()
    monkeypatch.setattr(specific, "read_context_via_text_pattern", lambda: None)
    copies = 0

    def switch_after_copy():
        nonlocal copies
        copies += 1
        if copies == copy_number:
            desktop.foreground = 200

    desktop.after_copy = switch_after_copy
    _, standard = strategy_pair(desktop)
    result = standard.clipboard_strategy.insert("hello", context)
    assert not result.success
    assert all(target == 100 for _, target, _ in desktop.sends)


def test_flutter_capture_must_belong_to_the_foreground_before_sendkeys(desktop):
    desktop.top_class = "FLUTTER_RUNNER_WIN32_WINDOW"
    desktop.foreground = 200
    context = context_module.capture_context()
    assert context.is_flutter
    _, standard = strategy_pair(desktop)
    result = standard.shadow_strategy.insert("hello", context)
    assert not result.success
    assert desktop.sends == []


def test_rejected_text_keeps_the_original_identity_when_uia_turns_stale(desktop):
    from queue import Queue
    from ui.rejection_text_cache import RejectionTextCache
    from ui.text_target import TextTargetVerdict

    context = context_module.capture_context()
    identity = context.target_identity
    desktop.stale = True
    queue = Queue()
    cache = RejectionTextCache()
    rejected = specific.RejectedInsertionStrategy(response_queue=queue, text_cache=cache)
    rejected.set_pending_verdict(TextTargetVerdict(
        verdict=False, reason="default_reject_paste_capable_class",
        supported_patterns=("Invoke",), control_type="Pane",
        class_name="unknown::editor", process_name="unknown.exe",
    ))
    result = rejected.insert("recover these words", context)
    event = queue.get_nowait()
    stored = cache.resolve(event["correlation_token"])
    assert result.was_rejected
    assert stored.text == "recover these words"
    assert (stored.target_hwnd, stored.target_process_id, stored.target_root,
            stored.target_tag) == (identity.hwnd, identity.process_id, identity.root, identity.tag)
    assert "recover these words" not in repr(event)
    assert desktop.sends == []
    assert desktop.clipboard == "original"


def test_unresolved_target_refusal_keeps_error_feedback(desktop, caplog):
    import logging

    desktop.stale = True
    context = context_module.capture_context()
    _, standard = strategy_pair(desktop)
    with caplog.at_level(logging.ERROR):
        result = standard.shadow_strategy.insert("hello", context)
    assert not result.success
    assert any(record.levelno == logging.ERROR and "captured target" in record.message
               for record in caplog.records)
    assert desktop.sends == []


@pytest.mark.parametrize("change", ["none", "same_pid_foreground", "foreground_recycle"])
def test_browser_helper_keeps_only_the_original_foreground_object(desktop, monkeypatch, change):
    desktop.windows[41] = (41, 7)  # Invisible browser helper has its own root.
    desktop.windows[200] = (200, 7)
    monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "brave.exe"))
    monkeypatch.setattr(hwnd_utils.win32gui, "IsWindowVisible", lambda hwnd: hwnd != 41)
    monkeypatch.setattr(hwnd_utils.win32gui, "GetWindowLong", lambda *args: 0)
    context = context_module.capture_context()
    assert context.target_identity.root == 41
    assert context.target_identity.foreground_root == 100

    def change_after_copy():
        if change == "same_pid_foreground":
            desktop.foreground = 200
        elif change == "foreground_recycle":
            desktop.markers.pop(100, None)

    desktop.after_copy = change_after_copy
    _, standard = strategy_pair(desktop)
    result = standard.shadow_strategy.insert("hello", context)
    assert result.success is (change == "none")
    assert desktop.sends == ([("paste", 100, "Hello")] if change == "none" else [])


def test_browser_probe_cannot_leave_a_stale_foreground_proof_before_paste(desktop, monkeypatch):
    desktop.windows[41] = (41, 7)
    desktop.windows[200] = (200, 7)
    armed = False

    def process_name():
        if armed:
            desktop.foreground = 200
        return "brave.exe"

    monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=process_name))
    monkeypatch.setattr(hwnd_utils.win32gui, "IsWindowVisible", lambda hwnd: hwnd != 41)
    monkeypatch.setattr(hwnd_utils.win32gui, "GetWindowLong", lambda *args: 0)
    context = context_module.capture_context()
    original_check = desktop.ops._captured_identity_is_current
    checks = 0

    def check_after_preparation(identity):
        nonlocal checks, armed
        checks += 1
        # The first check runs before clipboard preparation. The final one
        # performs the real process-name fallback; switch during that probe.
        armed = checks == 2
        return original_check(identity)

    monkeypatch.setattr(desktop.ops, "_captured_identity_is_current", check_after_preparation)
    _, standard = strategy_pair(desktop)
    result = standard.shadow_strategy.insert("hello", context)
    assert not result.success
    assert desktop.sends == []
    assert desktop.ops.last_paste_was_sent is False


@pytest.mark.parametrize("change", ["return", "destroy", "recycle", "new_helper", "sibling"])
def test_browser_focus_return_to_captured_root_keeps_original_provenance(desktop, monkeypatch, change):
    from ui.window_focus_manager import WindowFocusManager

    # UIA identifies the main frame while a still-live browser helper owns
    # foreground. The normal focus proof returns to that exact captured frame.
    desktop.windows[300] = (300, 7)
    desktop.windows[400] = (400, 7)
    desktop.windows[200] = (200, 7)
    desktop.foreground = 300
    monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "brave.exe"))
    monkeypatch.setattr(hwnd_utils.win32gui, "IsWindowVisible", lambda hwnd: hwnd not in (300, 400))
    monkeypatch.setattr(hwnd_utils.win32gui, "GetWindowLong", lambda *args: 0)
    monkeypatch.setattr(hwnd_utils.win32gui, "IsIconic", lambda hwnd: False)
    focus_calls = []

    def foreground(hwnd):
        focus_calls.append(("foreground", hwnd))
        desktop.foreground = hwnd

    def control_focus():
        focus_calls.append(("control", desktop.foreground))

    monkeypatch.setattr(hwnd_utils.win32gui, "SetForegroundWindow", foreground)
    monkeypatch.setattr(desktop.control, "SetFocus", control_focus)
    desktop.window_manager = WindowFocusManager(["brave.exe"])
    context = context_module.capture_context()
    identity = context.target_identity
    assert (identity.root, identity.foreground_root) == (100, 300)

    def after_focus_proof():
        if change == "destroy":
            desktop.windows.pop(300)
            # Win32 removes window-object properties when the window dies.
            desktop.markers.pop(300)
        elif change == "recycle":
            desktop.markers.pop(300)
        elif change == "new_helper":
            desktop.foreground = 400
        elif change == "sibling":
            desktop.foreground = 200

    desktop.before_unicode = after_focus_proof
    unicode, standard = strategy_pair(desktop)
    result = specific.UnicodeFirstStrategy(unicode, standard, desktop.ops).insert("hello", context)

    assert focus_calls == [("foreground", 100), ("control", 100)]
    if change == "return":
        assert identity.is_current(require_foreground=False)
    assert result.success is (change == "return"), "return to the original live target must deliver exactly once"
    assert desktop.sends == ([("unicode", 100, "Hello")] if change == "return" else [])
    assert desktop.ops.last_paste_was_sent is (change == "return")

@pytest.mark.parametrize("route", ["standard", "unicode_first"])
@pytest.mark.parametrize("change", ["none", "after_copy", "after_send"])
def test_strategy_composition_retains_dirty_clipboard_after_helper_loss(desktop, monkeypatch, route, change):
    # The original main target stays live while its captured browser helper
    # can disappear after the shadow strategy has copied the dictated text.
    desktop.windows[300] = (300, 7)
    desktop.foreground = 300
    monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "brave.exe"))
    monkeypatch.setattr(hwnd_utils.win32gui, "IsWindowVisible", lambda hwnd: hwnd != 300)
    monkeypatch.setattr(hwnd_utils.win32gui, "GetWindowLong", lambda *args: 0)
    context = context_module.capture_context()
    assert (context.target_identity.root, context.target_identity.foreground_root) == (100, 300)

    copies = []

    def after_copy():
        copies.append(desktop.clipboard)
        if change == "after_copy":
            desktop.windows.pop(300)
            desktop.markers.pop(300)
            desktop.foreground = 100

    desktop.after_copy = after_copy
    original_send = clipboard_module.verified_press_keys

    def send(*keys):
        outcome = original_send(*keys)
        if change == "after_send":
            desktop.foreground = 200
        return outcome

    monkeypatch.setattr(clipboard_module, "verified_press_keys", send)
    unicode, standard = strategy_pair(desktop)
    fallback_results = []
    original_fallback = standard.clipboard_strategy.insert

    def fallback(*args, **kwargs):
        outcome = original_fallback(*args, **kwargs)
        fallback_results.append(outcome)
        return outcome

    monkeypatch.setattr(standard.clipboard_strategy, "insert", fallback)
    strategy = standard
    if route == "unicode_first":
        # Real Unicode strategy refuses before SendInput, leaving Standard
        # to perform the clipboard attempt through its real inner strategies.
        unicode.window_manager = SimpleNamespace(ensure_focused=lambda hwnd: False)
        strategy = specific.UnicodeFirstStrategy(unicode, standard, desktop.ops)

    result = strategy.insert("hello", context)
    assert copies == ["Hello"]
    assert desktop.clipboard == "Hello"
    assert result.success is (change == "none")
    assert result.retry_outcome == "n/a"
    if change == "after_copy":
        assert desktop.sends == []
        assert desktop.ops.last_paste_was_sent is False
        assert desktop.buffer._buffer == ""
        assert len(fallback_results) == 1
        final_refusal = fallback_results[0]
        assert final_refusal.clipboard_dirty is False
        assert result.success is final_refusal.success is False
        assert result.rejected_reason == final_refusal.rejected_reason == "captured_target_lost"
        assert result.retry_outcome == final_refusal.retry_outcome
    else:
        assert desktop.sends == [("paste", 300, "Hello")]
        assert desktop.ops.last_paste_was_sent is True
        assert result.rejected_reason is None
        # Success and a post-send failure both skip the slow fallback.
        assert fallback_results == []
    assert result.clipboard_dirty is True, "earlier clipboard writes survive the composed result"


def _stale_focus_strategy_desktop(desktop, monkeypatch, scenario):
    from _ctypes import COMError
    from ui.window_focus_manager import WindowFocusManager

    # Capture the main frame while its original, still-live browser helper
    # owns foreground. UIA SetFocus subsequently raises from the saved element.
    desktop.windows.update({300: (300, 7), 400: (400, 7), 200: (200, 7)})
    desktop.foreground = 300
    monkeypatch.setattr(context_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "brave.exe"))
    monkeypatch.setattr(hwnd_utils.win32gui, "IsWindowVisible", lambda hwnd: hwnd not in (300, 400))
    monkeypatch.setattr(hwnd_utils.win32gui, "GetWindowLong", lambda *args: 0)
    monkeypatch.setattr(hwnd_utils.win32gui, "IsIconic", lambda hwnd: False)
    calls = []

    def foreground(hwnd):
        calls.append(("foreground", hwnd))
        desktop.foreground = 200 if scenario == "activation_refused" else hwnd

    def control_focus():
        calls.append(("control", desktop.foreground))
        if scenario == "destroy_target":
            desktop.windows.pop(41, None)
            desktop.markers.pop(41, None)
        elif scenario == "recycle_target":
            desktop.markers.pop(41, None)
        elif scenario == "recycle_root":
            desktop.markers.pop(100, None)
        elif scenario == "changed_pid":
            desktop.windows[41] = (100, 8)
        elif scenario == "reparent_target":
            desktop.windows[41] = (200, 7)
        elif scenario == "lost_original_foreground":
            desktop.markers.pop(300, None)
        elif scenario == "sibling_foreground":
            desktop.foreground = 200
        elif scenario == "new_helper_foreground":
            desktop.foreground = 400
        elif scenario == "original_helper_foreground":
            desktop.foreground = 300
        elif scenario == "foreground_error":
            def unavailable():
                raise OSError("synthetic foreground read failed")
            monkeypatch.setattr(hwnd_utils.win32gui, "GetForegroundWindow", unavailable)
        if scenario == "ordinary_com_error":
            raise COMError(-2147467259, "synthetic E_FAIL", (None, None, None, 0, None))
        if scenario == "ordinary_error":
            raise RuntimeError("synthetic SetFocus failure")
        if scenario == "lookalike_hresult":
            error = RuntimeError("synthetic non-COM failure")
            error.hresult = -2147220991
            raise error
        raise COMError(-2147220991, "synthetic unavailable UIA element", (None, None, None, 0, None))

    monkeypatch.setattr(hwnd_utils.win32gui, "SetForegroundWindow", foreground)
    monkeypatch.setattr(desktop.control, "SetFocus", control_focus)
    desktop.window_manager = WindowFocusManager(["brave.exe"])
    context = context_module.capture_context()
    assert (context.target_identity.hwnd, context.target_identity.root,
            context.target_identity.process_id, context.target_identity.foreground_root) == (41, 100, 7, 300)
    return context, calls


@pytest.mark.parametrize("route", ["unicode_first", "standard"])
@pytest.mark.parametrize("scenario", [
    "stale", "ordinary_com_error", "ordinary_error", "lookalike_hresult",
    "activation_refused", "destroy_target", "recycle_target", "recycle_root",
    "changed_pid", "reparent_target", "lost_original_foreground",
    "sibling_foreground", "new_helper_foreground", "original_helper_foreground",
    "foreground_error", "legacy_context",
])
def test_stale_focus_recovery_requires_original_identity_and_exact_foreground(
    desktop, monkeypatch, route, scenario,
):
    context, calls = _stale_focus_strategy_desktop(desktop, monkeypatch, scenario)
    if scenario == "legacy_context":
        context.target_identity = None
    unicode, standard = strategy_pair(desktop)
    strategy = (specific.UnicodeFirstStrategy(unicode, standard, desktop.ops)
                if route == "unicode_first" else standard)
    # Verbatim traverses the real composition/focus/delivery paths without
    # unrelated text-context selection keystrokes after the intentional red.
    options = specific.InsertionOptions(mode=specific.InsertionMode.VERBATIM)
    result = strategy.insert("hello", context, options=options)

    expected = scenario == "stale"
    assert result.success is expected, "only a stale UIA element with the original live foreground may recover"
    kind = "unicode" if route == "unicode_first" else "paste"
    assert desktop.sends == ([(kind, 100, "hello")] if expected else [])
    assert desktop.ops.last_paste_was_sent is expected
    if scenario == "activation_refused":
        assert not any(call[0] == "control" for call in calls)
    else:
        assert ("control", 100) in calls, "the actual captured SetFocus must have raised"
    if expected:
        assert calls == [("foreground", 100), ("control", 100)]


def test_stale_focus_recovery_does_not_change_uncaptured_legacy_paste(desktop, monkeypatch):
    _, calls = _stale_focus_strategy_desktop(desktop, monkeypatch, "stale")
    monkeypatch.setattr(desktop.window_manager, "get_target_window", lambda control: (100, desktop.control))
    # No captured HWND/control/identity: preserve this pre-existing legacy
    # branch, which logs a SetFocus exception and attempts the resolved paste.
    assert desktop.ops.verified_paste("hello", desktop.window_manager)
    assert calls == [("foreground", 100), ("control", 100)]
    assert desktop.sends == [("paste", 100, "hello")]


# ---------------------------------------------------------------------------
# Direct coverage of the two functions capture_context's failed-read path uses
# (wh-insert-focus-read-stall.1.3). Every other test in the tree reaches them
# only through capture_context, which mocks them out, so a regression that
# returned a half-filled identity, skipped the completed-identity recheck, or
# dropped the foreground re-derivation would pass the whole suite while
# changing which words get pasted after a failed focused-control read. A
# half-filled identity is the damaging shape: it flows past the router's
# empty-identity check and is refused at verified_paste with an ERROR notice.
# These run on the same deterministic desktop fixture as the tests above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("foreground", [100, 41], ids=["own-root", "child-root"])
def test_capture_foreground_identity_records_the_live_foreground_window(desktop, foreground):
    # 41's GA_ROOT is 100 in this desktop, so the second case proves the
    # record keeps the raw foreground handle AND its normalized root apart.
    desktop.foreground = foreground
    identity = target_identity_module.capture_foreground_identity()
    assert (identity.hwnd, identity.root, identity.process_id) == (foreground, 100, 7)
    assert identity.tag == desktop.markers[foreground] != 0
    assert identity.root_tag == desktop.markers[100] != 0
    assert (identity.foreground_root, identity.foreground_tag) == (100, identity.root_tag), (
        "the target IS the foreground here, so the foreground slots repeat "
        "the root and its marker"
    )
    assert identity.is_current()


def test_capture_foreground_identity_is_empty_when_the_marker_cannot_be_written(
    desktop, monkeypatch,
):
    # The elevated-window case the function's own docstring names: UIPI blocks
    # SetProp against a higher-integrity window, so the marker reads 0 and the
    # identity cannot prove the window object later.
    monkeypatch.setattr(hwnd_utils._user32, "SetPropW",
                        lambda hwnd, key, value: False)
    assert (target_identity_module.capture_foreground_identity()
            == target_identity_module.TargetIdentity()), (
        "an untaggable foreground window must give the empty record, not a "
        "record with a zero marker: a partly filled identity flows past the "
        "router's empty-identity check and is refused at verified_paste with "
        "an ERROR notice"
    )
    assert desktop.markers == {}


def test_capture_foreground_identity_is_empty_when_the_foreground_moves_during_the_capture(
    desktop, monkeypatch,
):
    reads = []

    def foreground():
        reads.append(1)
        return 100 if len(reads) == 1 else 200

    monkeypatch.setattr(hwnd_utils.win32gui, "GetForegroundWindow", foreground)
    assert (target_identity_module.capture_foreground_identity()
            == target_identity_module.TargetIdentity()), (
        "a foreground that moved between the first probe and the completed "
        "identity must give the empty record, never a record naming the "
        "window the user left"
    )
    assert len(reads) == 2, (
        "the completed identity must be rechecked against a FRESH foreground "
        "read, not against the handle the capture started from"
    )


@pytest.mark.parametrize("broken", ["pid", "root", "window"])
def test_capture_foreground_identity_is_empty_on_any_partial_probe_failure(
    desktop, monkeypatch, broken,
):
    if broken == "pid":
        monkeypatch.setattr(hwnd_utils.win32process,
                            "GetWindowThreadProcessId", lambda hwnd: (1, 0))
    elif broken == "root":
        monkeypatch.setattr(hwnd_utils.win32gui, "GetAncestor",
                            lambda hwnd, flag: 0)
    else:
        desktop.windows.pop(100)
    assert (target_identity_module.capture_foreground_identity()
            == target_identity_module.TargetIdentity()), (
        "every probe must succeed or the whole record is empty; a record with "
        "one zero field is exactly the half-filled identity is_current's "
        "all-seven-nonzero check exists to refuse"
    )


def test_current_foreground_root_normalizes_the_foreground_and_tags_nothing(desktop):
    desktop.foreground = 41
    assert target_identity_module.current_foreground_root() == 100, (
        "the probe must report the GA_ROOT of the foreground window, not its "
        "raw handle, or a Chromium child window never matches its own root"
    )
    assert desktop.markers == {}, (
        "the probe captures nothing and tags nothing; tagging here would make "
        "the second capture that capture_target_identity's rule forbids"
    )


def test_current_foreground_root_is_zero_when_the_foreground_window_is_gone(desktop):
    desktop.foreground = 999
    assert target_identity_module.current_foreground_root() == 0, (
        "GetAncestor returns 0 for a destroyed handle; the caller compares "
        "this against a captured root, so it must fail closed with 0"
    )


def test_current_foreground_root_is_zero_when_the_foreground_read_raises(
    desktop, monkeypatch,
):
    def unavailable():
        raise OSError("synthetic foreground read failed")

    monkeypatch.setattr(hwnd_utils.win32gui, "GetForegroundWindow", unavailable)
    assert target_identity_module.current_foreground_root() == 0
