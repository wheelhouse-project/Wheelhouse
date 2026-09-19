"""capture_context() survives a focused-control read that blocks and fails.

wh-insert-focus-read-stall. On 2026-09-18 utterance 278 lost a word in Brave:
``auto.GetFocusedControl()`` blocked for 5.5 seconds and then raised COMError
(-2147220991). ui/context.py called it outside any try, so the error reached
the broad handler in ui/ui_action_handler.py, which logged at ERROR with a
traceback and dropped the word.

The boss ruled the behaviour on 2026-09-18 14:29, and revised the first rule
at 15:33 on finding wh-insert-focus-read-stall.1.1:

* Read the focused control exactly once per call and never retry a failure.
  The withdrawn rule retried a failure that returned quickly. That retry could
  not be bounded, because the entry test can only measure how long the FIRST
  call took: a failure returning in 0.49 seconds followed by a second call
  blocking 5.5 seconds passes the 5.0 second request limit in app.py line
  1409, which this change may not touch, and that timeout is logged at ERROR,
  which shows the user a Windows notice. No log file on record shows a second
  read ever succeeding after a first one failed.
* Capture the foreground window identity BEFORE the first read. When the read
  fails and the foreground has not moved, hand that earlier identity on, so
  the router pastes the word instead of dropping it. When the foreground HAS
  moved, give the empty identity, which the existing branch in ui/router.py
  drops silently -- a sentence pasted into a window the user did not dictate
  into is worse than one lost word.
* The identity on the failure path comes only from the earlier capture, never
  from one made after the failure, which keeps capture_target_identity's
  "an empty identity forbids recapture" rule intact.
"""
import logging
from unittest import mock

import pytest  # noqa: F401  (pytest collects this module)

import ui.context as context_mod
from ui import elevation_check
from ui.context import UIContext
from ui.router import InsertionRouter
from ui.target_identity import TargetIdentity


# A stand-in for the COMError the report recorded. capture_context catches
# Exception, so the test does not need comtypes to exercise the path.
class _FakeComError(Exception):
    pass


_COM_ERROR = _FakeComError(
    "(-2147220991, 'An event was unable to invoke any of the subscribers')"
)

# A foreground identity that is not the empty record, so the router's
# empty-identity drop does not fire on it.
_PRE_READ_IDENTITY = TargetIdentity(
    hwnd=0x1111, root=0x1111, process_id=4321, tag=7,
    root_tag=7, foreground_root=0x1111, foreground_tag=7,
)

# The provenance markers those handles carry while the window object the user
# dictated into is still alive. capture_context re-reads these after the root
# compare, because Windows reuses top-level handles: a numeric root compare
# alone cannot tell the original window from a new one born on the same
# handle, and only the SetProp marker dies with the window object.
_LIVE_MARKERS = {0x1111: 7}

# A pre-read identity whose focused window and its root are DIFFERENT handles,
# so a test can lose one marker without losing the other.
_CHILD_IDENTITY = TargetIdentity(
    hwnd=0x1112, root=0x1111, process_id=4321, tag=5,
    root_tag=7, foreground_root=0x1111, foreground_tag=7,
)

_CHILD_MARKERS = {0x1111: 7, 0x1112: 5}


class _FakeControl:
    """Minimal stand-in for a uiautomation focused control."""

    def __init__(self, class_name="Chrome_RenderWidgetHostHWND"):
        self.ClassName = class_name
        self.ProcessId = 4321

    def GetTopLevelControl(self):
        top = mock.MagicMock()
        top.ClassName = "Chrome_WidgetWin_1"
        return top


def _run_capture(
    *,
    read_results,
    foreground_root_after=0x1111,
    pre_read_identity=_PRE_READ_IDENTITY,
    markers=None,
):
    """Run the real capture_context() against a scripted focused-control read.

    ``read_results`` is one entry per call to GetFocusedControl: either an
    exception instance to raise or a control to return. capture_context must
    consume exactly the first entry, because it reads once and never retries;
    a test that supplies more than one entry proves that by the returned call
    count, not by the entries being used.

    ``pre_read_identity`` is what capture_foreground_identity returns, so a
    test can drive the branch where no identity could be captured at all.
    ``markers`` is the handle-to-provenance-marker map the patched
    ``read_hwnd_provenance`` answers from; a handle missing from it reads 0,
    which is what a destroyed or recycled window object gives. It defaults to
    the markers that keep ``pre_read_identity`` valid.

    Returns (context, number of GetFocusedControl calls).
    """
    if markers is None:
        markers = {
            pre_read_identity.root: pre_read_identity.root_tag,
            pre_read_identity.hwnd: pre_read_identity.tag,
        }
    calls = {"n": 0}

    def _read():
        calls["n"] += 1
        result = read_results[calls["n"] - 1]
        if isinstance(result, BaseException):
            raise result
        return result

    proc = mock.MagicMock()
    proc.name.return_value = "brave.exe"
    with mock.patch.object(context_mod.auto, "GetFocusedControl", _read), \
         mock.patch.object(context_mod.psutil, "Process", return_value=proc), \
         mock.patch.object(
             context_mod, "capture_foreground_identity",
             return_value=pre_read_identity,
         ), \
         mock.patch.object(
             context_mod, "current_foreground_root",
             return_value=foreground_root_after,
         ), \
         mock.patch.object(
             context_mod, "read_hwnd_provenance",
             side_effect=lambda hwnd: markers.get(hwnd, 0),
         ), \
         mock.patch.object(
             context_mod, "capture_target_identity",
             return_value=TargetIdentity(
                 hwnd=0x2222, root=0x2222, process_id=4321, tag=9,
                 root_tag=9, foreground_root=0x2222, foreground_tag=9,
             ),
         ):
        return context_mod.capture_context(), calls["n"]


def _records_at(caplog, level):
    return [r for r in caplog.records if r.levelno == level]


class TestExactlyOneFocusedControlRead:
    """A1b: capture_context reads once per call and never retries.

    The boss ruled this on 2026-09-18 (finding
    wh-insert-focus-read-stall.1.1), withdrawing an earlier ruling that
    retried a failure which returned quickly. A retry cannot be bounded: the
    entry test can only measure the FIRST call, so a failure returning in 0.49
    seconds followed by a second call blocking 5.5 seconds passes the 5.0
    second request limit in app.py and fires the timeout ERROR, which is a
    user notification. It would also hold the Input process's single command
    loop for the whole second call.

    Each test here supplies a SECOND read result that would succeed. A
    capture_context that retried would consume it and pass the old
    assertions, so the call count is what these tests measure.
    """

    def test_a_failed_read_is_never_retried(self, caplog):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        context, reads = _run_capture(
            read_results=[_COM_ERROR, _FakeControl()],
        )
        assert reads == 1, (
            "a failed focused-control read must not start a second one; "
            "nothing can bound the second call once it has started"
        )
        assert context.focused_control is None
        assert context.focus_read_failed is True

    def test_a_successful_read_happens_exactly_once(self, caplog):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        control = _FakeControl()
        context, reads = _run_capture(
            read_results=[control, _FakeControl()],
        )
        assert reads == 1
        assert context.focused_control is control
        assert context.focus_read_failed is False
        assert context.class_name == "Chrome_RenderWidgetHostHWND"
        assert not _records_at(caplog, logging.WARNING)

    def test_no_retry_limit_constant_survives(self):
        assert not hasattr(context_mod, "_FOCUS_READ_RETRY_LIMIT_SECONDS"), (
            "the retry limit constant must be gone, not left at a value that "
            "makes the retry unreachable; a later reader would restore it"
        )


class TestFailedReadKeepsTheWordWhenTheForegroundHeld:
    """A2: both reads fail, the foreground did not move, the word is kept."""

    def test_the_identity_is_the_capture_made_before_the_read(self, caplog):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        context, reads = _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=0x1111,
        )
        assert reads == 1, "one read per call, never a retry"
        assert context.focused_control is None
        assert context.focus_read_failed is True
        assert context.target_identity == _PRE_READ_IDENTITY, (
            "the failure path must reuse the pre-read capture, so the router "
            "pastes rather than taking its empty-identity drop"
        )
        assert context.target_identity != TargetIdentity()

    def test_one_warning_names_the_com_error_and_no_error_is_logged(
        self, caplog,
    ):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=0x1111,
        )
        warnings = _records_at(caplog, logging.WARNING)
        assert len(warnings) == 1, f"expected one WARNING, got {warnings}"
        assert "-2147220991" in warnings[0].getMessage()
        assert not _records_at(caplog, logging.ERROR), (
            "an ERROR record reaches ErrorNotificationHandler and shows the "
            "user a Windows notice"
        )
        assert warnings[0].exc_info is None, "no traceback on this path"


class TestFailedReadDropsTheWordWhenTheForegroundMoved:
    """A2b: both reads fail and the foreground moved, so the word is dropped."""

    def test_a_moved_foreground_gives_the_empty_identity(self, caplog):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        context, reads = _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=0x9999,
        )
        assert reads == 1, "one read per call, never a retry"
        assert context.focused_control is None
        assert context.focus_read_failed is True
        assert context.target_identity == TargetIdentity(), (
            "a moved foreground must produce the empty record, which "
            "ui/router.py already drops silently; handing the earlier "
            "identity on would paste into a window the user did not "
            "dictate into"
        )

    def test_one_warning_says_the_target_window_changed(self, caplog):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=0x9999,
        )
        warnings = _records_at(caplog, logging.WARNING)
        assert len(warnings) == 1, f"expected one WARNING, got {warnings}"
        message = warnings[0].getMessage()
        assert "target window changed" in message.lower(), (
            "\"unchanged\" contains \"changed\", so the weaker check passed "
            "under the mutation that removes the moved-foreground branch"
        )
        assert not _records_at(caplog, logging.ERROR)


class TestFailedReadDropsTheWordWhenTheWindowObjectWasRecycled:
    """A2c: the root handle survived the read but the window object did not.

    wh-insert-focus-read-stall.1.2. Windows reuses top-level handles, so a new
    window can hold the foreground on the same numeric root the pre-read
    identity recorded. The root compare passes, the identity is handed on, and
    ``verified_paste`` then refuses it at ERROR -- a Windows notice on a path
    that promises no notice. The provenance marker is the one probe a same-run
    handle reuse cannot alias (``hwnd_utils.tag_hwnd_provenance``), so
    capture_context re-reads it before it chooses the paste.
    """

    @pytest.mark.parametrize("lost", ["root", "hwnd", "both"])
    def test_a_lost_provenance_marker_gives_the_empty_identity(
        self, caplog, lost,
    ):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        markers = dict(_CHILD_MARKERS)
        if lost in ("root", "both"):
            markers.pop(_CHILD_IDENTITY.root)
        if lost in ("hwnd", "both"):
            markers.pop(_CHILD_IDENTITY.hwnd)
        context, reads = _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=_CHILD_IDENTITY.root,
            pre_read_identity=_CHILD_IDENTITY,
            markers=markers,
        )
        assert reads == 1, "one read per call, never a retry"
        assert context.focus_read_failed is True
        assert context.target_identity == TargetIdentity(), (
            "the numeric root compare cannot tell a recycled handle from the "
            "original window; a lost provenance marker must take the "
            "empty-identity drop rather than hand the stale identity to a "
            "paste that verified_paste refuses at ERROR"
        )

    def test_one_warning_reports_the_recycled_window_and_no_error_is_logged(
        self, caplog,
    ):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=_CHILD_IDENTITY.root,
            pre_read_identity=_CHILD_IDENTITY,
            markers={},
        )
        warnings = _records_at(caplog, logging.WARNING)
        assert len(warnings) == 1, f"expected one WARNING, got {warnings}"
        message = warnings[0].getMessage()
        assert "-2147220991" in message
        assert "window object was replaced" in message.lower(), (
            "the WARNING must say the window object is gone, so this branch "
            "is distinguishable in a production log from the branch that "
            "reports the target window as unchanged and pastes"
        )
        assert not _records_at(caplog, logging.ERROR), (
            "an ERROR record reaches ErrorNotificationHandler and shows the "
            "user a Windows notice; this path promises none"
        )
        assert warnings[0].exc_info is None, "no traceback on this path"

    def test_the_markers_are_re_read_after_the_root_compare(self, caplog):
        """The live markers keep the paste, so the re-check is not a blanket
        drop of every failed read that got this far."""
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        context, _ = _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            foreground_root_after=_CHILD_IDENTITY.root,
            pre_read_identity=_CHILD_IDENTITY,
            markers=dict(_CHILD_MARKERS),
        )
        assert context.target_identity == _CHILD_IDENTITY
        assert not _records_at(caplog, logging.ERROR)


class TestFailedReadWithNoIdentityCapturedBeforeIt:
    """A2d: branch 1 -- the read failed and the pre-read capture was empty.

    wh-insert-focus-read-stall.1.3. ``capture_foreground_identity`` returns the
    empty record whenever the foreground window cannot be tagged, which its own
    docstring names for an elevated window: UIPI blocks the SetProp, the marker
    reads 0, and the identity fails ``is_current()``. There is nothing to paste
    into, so the word is dropped with one WARNING -- an ERROR here would reach
    ErrorNotificationHandler and show the user a Windows notice.
    """

    def test_an_empty_pre_read_capture_drops_the_word_with_one_warning(
        self, caplog,
    ):
        caplog.set_level(logging.DEBUG, logger=context_mod.logger.name)
        context, reads = _run_capture(
            read_results=[_COM_ERROR, _COM_ERROR],
            pre_read_identity=TargetIdentity(),
            markers={},
        )
        assert reads == 1, "one read per call, never a retry"
        assert context.focused_control is None
        assert context.focus_read_failed is True
        assert context.target_identity == TargetIdentity(), (
            "no identity existed before the read, so the empty record must be "
            "handed on for ui/router.py's silent drop"
        )
        warnings = _records_at(caplog, logging.WARNING)
        assert len(warnings) == 1, f"expected one WARNING, got {warnings}"
        message = warnings[0].getMessage()
        assert "-2147220991" in message, (
            "the one WARNING must name the read error that lost the word"
        )
        assert "no target window identity" in message, (
            "the WARNING must say WHY the word was dropped, so this branch is "
            "distinguishable from the moved-foreground and recycled-window "
            "branches in a production log"
        )
        assert not _records_at(caplog, logging.ERROR), (
            "an ERROR record reaches ErrorNotificationHandler and shows the "
            "user a Windows notice; this path promises none"
        )
        assert warnings[0].exc_info is None, "no traceback on this path"


class TestExistingCallersAreUnchanged:
    """A4: a successful read behaves exactly as it did before the change."""

    def test_a_normal_capture_carries_the_control_identity(self):
        control = _FakeControl()
        context, reads = _run_capture(
            read_results=[control],
        )
        assert reads == 1
        assert context.target_identity.hwnd == 0x2222, (
            "the happy path must keep using capture_target_identity's "
            "control-derived identity, not the foreground capture"
        )
        assert context.focus_read_failed is False


def _elevated_foreground():
    """Patches that make the foreground window an elevated one.

    No UIA control is involved: every value comes from the window handle,
    which is the whole point of the fallback this exercises.
    """
    return (
        mock.patch.object(
            elevation_check.win32gui, "GetForegroundWindow",
            return_value=0x1111,
        ),
        mock.patch.object(
            elevation_check.win32process, "GetWindowThreadProcessId",
            return_value=(0, 4321),
        ),
        mock.patch.object(
            elevation_check, "_own_integrity_rid", return_value=0x2000,
        ),
        mock.patch.object(
            elevation_check, "_integrity_rid_of_pid", return_value=0x3000,
        ),
    )


class TestAdministratorRefusalSurvivesAFailedRead:
    """A3: the administrator refusal still fires when the read failed.

    The boss ruled this a test-only item on 2026-09-18: no code change is
    owed, because elevation_check._resolve_target_hwnd already falls back to
    GetForegroundWindow() when the control gives no handle, and a failed read
    gives no control at all. These two tests are what keeps that true.
    """

    def test_the_foreground_fallback_still_reports_an_elevated_target(self):
        a, b, c, d = _elevated_foreground()
        with a, b, c, d:
            assert elevation_check.target_elevation_state(None) == (
                elevation_check.ELEVATED
            ), (
                "with no focused control the check must resolve the target "
                "through GetForegroundWindow, not give up"
            )

    def test_the_router_refuses_a_failed_read_context(self):
        rejected = mock.MagicMock()
        predicate = mock.MagicMock()
        router = InsertionRouter(
            standard_strategy=mock.MagicMock(),
            flutter_strategy=mock.MagicMock(),
            simple_paste_strategy=mock.MagicMock(),
            rejected_strategy=rejected,
            text_target_predicate=predicate,
            verified_unicode_strategy=mock.MagicMock(),
            verified_unicode_max_chars=50,
            clipboard_only_strategy=mock.MagicMock(),
            elevation_checker=elevation_check.target_elevation_state,
        )
        context = UIContext(
            focused_control=None,
            is_flutter=False,
            is_terminal=False,
            process_name="regedit.exe",
            class_name="RegEdit_RegEdit",
            process_id=4321,
            target_identity=_PRE_READ_IDENTITY,
            focus_read_failed=True,
        )
        a, b, c, d = _elevated_foreground()
        with a, b, c, d:
            assert router.get_strategy(context, "hello") is rejected
        predicate.evaluate.assert_not_called()
        verdict = rejected.set_pending_verdict.call_args[0][0]
        assert verdict.reason == "elevated_process_window"


class TestTheAcceptedSameWindowFocusLimit:
    """The paste proceeds with no control, and the SetFocus step is skipped.

    wh-insert-focus-read-stall.1.5 (codex round 3), closed under the
    boss's ruling of 2026-09-18 as an accepted limit, option A. The
    reviewer proposed dropping the word whenever the read failed, which
    would reverse this whole bead, so the limit is recorded instead of
    removed. The crewcut: comment in ui/context.py names it and names the
    removal path.

    What the limit is. On the ordinary path the code still holds the
    captured control object and calls ``resolved_control.SetFocus()``
    before the paste, at ui/clipboard_operations.py:526-528 and again at
    :857-859. That pulls focus back to the field the control names. A
    failed read leaves no control, so both calls are skipped, and a
    person who moves to another field of the SAME top-level window during
    the stalled read gets the word in the new field.

    What the limit is NOT. Neither path ever proved WHICH FIELD held
    focus: ``capture_target_identity`` stores the control's TOP-LEVEL
    window (ui/target_identity.py:112-135), so all seven fields of
    TargetIdentity are top-level facts and ``is_current()`` is blind to a
    field change either way. The ordinary path also captures the control
    that holds focus when ``auto.GetFocusedControl()`` RETURNS, not when
    the word was spoken, so after a slow successful read the word lands
    in the later field too. The failure path loses only the correction,
    for the interval between the read and the paste.

    This test pins the accepted behaviour so a later change cannot turn
    it into a refusal without someone reading this text first.
    """

    def _operations(self):
        from ui.clipboard_operations import ClipboardOperations

        return ClipboardOperations({
            "ui_actions": {
                "timing": {
                    "utterance_clipboard_timeout_seconds": 1.0,
                    "clipboard_verification_timeout_ms": 250,
                    "clipboard_operation_delay_ms": 50,
                    "selection_clear_delay_ms": 20,
                    "context_gather_delay_ms": 10,
                    "post_paste_delay_ms": 30,
                }
            }
        })

    def test_a_control_of_none_skips_setfocus_and_still_proves_the_window(
        self, caplog,
    ):
        operations = self._operations()
        window_manager = mock.MagicMock()
        window_manager.ensure_focused.return_value = True
        hwnd = 0x2222

        with mock.patch(
                "ui.clipboard_operations.win32gui.GetForegroundWindow",
                return_value=hwnd), \
             mock.patch.object(operations, "_foreground_matches_target",
                               return_value=True) as matches, \
             caplog.at_level(logging.DEBUG,
                             logger="ui.clipboard_operations"):
            proved = operations.prove_captured_target_is_foreground(
                window_manager, hwnd, None, caller="verified_paste",
            )

        assert proved is True, (
            "a failed read must still be allowed to paste into the "
            "window the pre-read capture named; refusing here would "
            "restore the dropped word this bead removed"
        )
        matches.assert_called_once()
        errors = [
            r for r in caplog.records
            if r.levelno >= logging.ERROR
            and r.name == "ui.clipboard_operations"
        ]
        assert errors == [], (
            "the skipped SetFocus step logged an error, which "
            "ErrorNotificationHandler would show the user as a box: "
            f"{[r.message for r in errors]}"
        )
