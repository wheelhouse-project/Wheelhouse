"""Tests for UIActionHandler capture/replace clipboard methods.

Tests capture_selected_text() and replace_selected_text() -- the Input Process
side of the AI "fix this" flow. These methods use the sentinel clipboard pattern
proven in transform_selection() to capture and replace text via clipboard IPC.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch, call

import pytest


# Patch paths
_MOD = "ui.ui_action_handler"


def _make_config(**overrides):
    """Build a minimal config dict for UIActionHandler."""
    cfg = {
        "ui_actions": {
            "timing": {
                "utterance_clipboard_timeout_seconds": 1.0,
                "clipboard_verification_timeout_ms": 250,
            }
        }
    }
    cfg.update(overrides)
    return cfg


@contextmanager
def _noop_clipboard_context(**kwargs):
    """No-op replacement for clipboard_context in tests."""
    yield


# wh-review-pattern-fixes.45: replace_selected_text refuses to paste
# unless the caller hands back the capture token and the HWND that
# capture_selected_text issued, and unless the captured target still
# holds the foreground. Every test that exercises the paste itself must
# arm both.
_CAPTURED_HWND = 4321


def _arm_capture_target(handler):
    """Seed the captured-target slot and let the proof pass.

    Returns the kwargs the Logic process hands back with the
    replacement text.
    """
    from ui.ui_action_handler import CapturedTarget

    token = "test-capture-token"
    handler._captured_selection_target = (
        token, CapturedTarget(control=MagicMock(), hwnd=_CAPTURED_HWND),
    )
    handler.clipboard.prove_captured_target_is_foreground.return_value = True
    return {"capture_token": token, "target_hwnd": _CAPTURED_HWND}


@pytest.fixture
def handler():
    """Create a UIActionHandler with specialist components mocked."""
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations") as MockCO, \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"), \
         patch(f"{_MOD}.capture_context"), \
         patch(f"{_MOD}.clipboard_context", side_effect=_noop_clipboard_context):

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        h = UIActionHandler(response_queue=q, config=_make_config())
        # Set clipboard verification timeout on the mock
        h.clipboard.clipboard_verification_timeout = 0.01  # fast for tests
        yield h


# =========================================================================
# capture_selected_text
# =========================================================================

class TestCaptureSelectedText:
    """Tests for UIActionHandler.capture_selected_text()."""

    def test_returns_selected_text(self, handler):
        """When text is selected, Ctrl+C copies it and it's returned."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time:
            # Sentinel first, then selected text appears after Ctrl+C
            mock_clip.paste.side_effect = ["selected text"]
            mock_time.time.side_effect = [1000.0, 1000.0, 1000.0]
            mock_time.sleep = Mock()

            result = handler.capture_selected_text()

            assert result["text"] == "selected text"

    def test_ctrl_c_sent(self, handler):
        """A verified Ctrl+C is sent to copy the current selection
        (wh-review-pattern-fixes.35: not fire-and-forget press_keys)."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}.time") as mock_time:
            mock_clip.paste.side_effect = ["captured"]
            mock_time.time.side_effect = [1000.0, 1000.0, 1000.0]
            mock_time.sleep = Mock()

            handler.capture_selected_text()

            # The copy goes through the verified sender.
            mock_vpk.assert_any_call('ctrl', 'c')
            assert call('ctrl', 'c') not in mock_keys.call_args_list

    def test_no_selection_selects_all(self, handler):
        """When no text is selected (sentinel unchanged), Ctrl+A then Ctrl+C."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}.time") as mock_time:
            sentinel = None  # Will be set by copy()

            def track_copy(text):
                nonlocal sentinel
                sentinel = text

            # wh-fz7j.4: capture_selected_text now routes its sentinel write
            # through self.clipboard._safe_copy. Forward the side effect so
            # the test's sentinel tracking and mock_clip.paste polling still work.
            handler.clipboard._safe_copy.side_effect = lambda t: (track_copy(t) or True)
            mock_clip.copy.side_effect = track_copy

            # First poll: sentinel unchanged (no selection)
            # After timeout, Ctrl+A+C, then poll returns text
            call_count = [0]

            def paste_side_effect():
                call_count[0] += 1
                if call_count[0] <= 3:
                    return sentinel  # Still sentinel (no selection)
                return "all text"  # After Ctrl+A, text appears

            mock_clip.paste.side_effect = paste_side_effect
            # Time progression: enough for first poll to timeout, then second succeeds
            mock_time.time.side_effect = [
                1000.0,  # sentinel creation
                1000.0, 1000.1, 1001.0,  # first poll: start, check, timeout
                1001.0, 1001.0, 1001.0,  # second poll: start, check, found
            ]
            mock_time.sleep = Mock()

            result = handler.capture_selected_text()

            # Ctrl+A goes through the verified sender
            # (wh-review-pattern-fixes.32 d), before the second Ctrl+C.
            mock_vpk.assert_any_call('ctrl', 'a')
            assert result["text"] == "all text"

    def test_no_text_anywhere_returns_empty(self, handler):
        """When even select-all yields nothing, return empty text."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time:
            sentinel = None

            def track_copy(text):
                nonlocal sentinel
                sentinel = text

            mock_clip.copy.side_effect = track_copy
            # Always returns sentinel -- no text in application at all
            mock_clip.paste.side_effect = lambda: sentinel
            # Time: both polls timeout
            time_values = [1000.0]  # sentinel creation
            time_values.extend([1000.0 + i * 0.5 for i in range(20)])  # all timeout
            mock_time.time.side_effect = time_values
            mock_time.sleep = Mock()

            result = handler.capture_selected_text()

            assert result["text"] == ""

    def test_uses_clipboard_context(self, handler):
        """Clipboard save/restore via clipboard_context."""
        with patch(f"{_MOD}.pyperclip") as mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time, \
             patch(f"{_MOD}.clipboard_context") as mock_ctx:
            mock_ctx.return_value.__enter__ = Mock(return_value=None)
            mock_ctx.return_value.__exit__ = Mock(return_value=False)
            mock_clip.paste.side_effect = ["text"]
            mock_time.time.side_effect = [1000.0, 1000.0, 1000.0]
            mock_time.sleep = Mock()

            handler.capture_selected_text()

            mock_ctx.assert_called_once()


class TestCaptureLetterBufferContract:
    """wh-review-pattern-fixes.26: capture flushes the deferred letter
    buffer before any clipboard or selection mutation, and a failed
    flush fails closed (no sentinel write, no Ctrl+C, no Ctrl+A)."""

    def test_flush_runs_before_sentinel_write_and_any_key(self, handler):
        order = []
        handler._letter_buffer = ["a"]
        handler._flush_letter_buffer = Mock(
            side_effect=lambda: order.append("flush") or True
        )
        handler.clipboard._safe_copy.side_effect = (
            lambda t: order.append("sentinel") or True
        )
        handler._poll_clipboard = Mock(return_value="selected text")
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys") as mock_vpk, \
             patch(f"{_MOD}.time"):
            mock_keys.side_effect = lambda *k: order.append("keys")
            mock_vpk.side_effect = (
                lambda *k: order.append("keys") or (True, 4, 4)
            )
            result = handler.capture_selected_text()

        assert order, "nothing was recorded"
        assert order[0] == "flush"
        assert result["text"] == "selected text"

    def test_failed_flush_fails_closed(self, handler):
        handler._letter_buffer = ["a"]
        handler._flush_letter_buffer = Mock(return_value=False)
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys") as mock_vpk, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["flush_failed"] is True
        assert result["text"] == ""
        handler.clipboard._safe_copy.assert_not_called()
        mock_keys.assert_not_called()
        mock_vpk.assert_not_called()

    def test_empty_buffer_reports_flush_ok(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value="selected text")
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["flush_failed"] is False


class TestCaptureFallbackFlag:
    """wh-review-pattern-fixes.28: the capture result reports whether
    the Ctrl+A no-selection fallback fired, so the Logic side can
    collapse the whole-field selection it leaves armed."""

    def test_direct_selection_reports_no_fallback(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value="selected text")
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["select_all_fallback"] is False

    def test_ctrl_a_fallback_reports_fallback(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(side_effect=[None, "all text"])
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        mock_vpk.assert_any_call('ctrl', 'a')
        assert result["select_all_fallback"] is True
        assert result["text"] == "all text"

    def test_failed_second_sentinel_write_still_reports_fallback(self, handler):
        """Ctrl+A already fired when the second sentinel write fails, so
        the armed whole-field selection must still be reported."""
        handler.clipboard._safe_copy.side_effect = [True, False]
        handler._poll_clipboard = Mock(return_value=None)
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["select_all_fallback"] is True
        assert result["text"] == ""


# =========================================================================
# replace_selected_text
# =========================================================================

class TestReplaceSelectedText:
    """Tests for UIActionHandler.replace_selected_text()."""

    def test_sets_clipboard_and_pastes(self, handler):
        """Text is placed on clipboard and a verified Ctrl+V is sent."""
        with patch(f"{_MOD}.pyperclip") as _mock_clip, \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()

            # wh-fz7j.4: replace_selected_text now routes the clipboard
            # write through self.clipboard._safe_copy. Configure the mock
            # so we can assert on it.
            handler.clipboard._safe_copy.return_value = True

            result = handler.replace_selected_text(
                text="corrected text",
                **_arm_capture_target(handler),
            )

            handler.clipboard._safe_copy.assert_called_with("corrected text")
            mock_vpk.assert_called_once_with('ctrl', 'v')
            assert result["success"] is True

    def test_returns_success(self, handler):
        """Returns success dict for IPC response."""
        with patch(f"{_MOD}.pyperclip"), \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            handler.clipboard._safe_copy.return_value = True

            result = handler.replace_selected_text(
                text="hello",
                **_arm_capture_target(handler),
            )

            assert isinstance(result, dict)
            assert result["success"] is True

    def test_invalidates_buffer(self, handler):
        """Buffer is invalidated since text content changed."""
        with patch(f"{_MOD}.pyperclip"), \
             patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()

            handler.replace_selected_text(
                text="replaced",
                **_arm_capture_target(handler),
            )

            handler.buffer_manager.invalidate.assert_called()


# =========================================================================
# wh-review-pattern-fixes.32 (d): verified Ctrl+A fallback arming
# =========================================================================

class TestCaptureVerifiedSelectAll:
    """capture_selected_text must not classify a possible user selection
    as a fallback selection on the strength of an UNVERIFIED Ctrl+A.
    press_keys discards the SendInput count; an undelivered Ctrl+A
    leaves the user's own selection active, and a later fallback
    collapse would move the user's caret. Only a verified Ctrl+A
    delivery may arm select_all_fallback."""

    def test_unverified_ctrl_a_does_not_arm_fallback(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value=None)
        # wh-review-pattern-fixes.35: the first Ctrl+C is verified too;
        # it must deliver here so the test still exercises the Ctrl+A
        # short-delivery specifically.
        vpk_results = {("ctrl", "c"): [(True, 4, 4)],
                       ("ctrl", "a"): [(False, 0, 4)]}
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   side_effect=_vpk_router(vpk_results)), \
             patch(f"{_MOD}._send_modifier_keyups", create=True), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["select_all_fallback"] is False
        assert result["text"] == ""
        # Ctrl+A must not go through fire-and-forget press_keys.
        assert call('ctrl', 'a') not in mock_keys.call_args_list
        # The fallback capture never ran: only the first sentinel write.
        assert handler.clipboard._safe_copy.call_count == 1

    def test_verified_ctrl_a_arms_fallback(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(side_effect=[None, "all text"])
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        mock_vpk.assert_any_call('ctrl', 'a')
        assert call('ctrl', 'a') not in mock_keys.call_args_list
        assert result["select_all_fallback"] is True
        assert result["text"] == "all text"

    def test_short_ctrl_a_releases_held_modifiers(self, handler):
        """A short chord can leave Ctrl physically held (the down event
        accepted, the up dropped). Release it before reporting."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value=None)
        # wh-review-pattern-fixes.35: the first Ctrl+C is verified too;
        # it must deliver here so the release under test is the Ctrl+A
        # one.
        vpk_results = {("ctrl", "c"): [(True, 4, 4)],
                       ("ctrl", "a"): [(False, 1, 4)]}
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   side_effect=_vpk_router(vpk_results)), \
             patch(f"{_MOD}._send_modifier_keyups", create=True) as mock_rel, \
             patch(f"{_MOD}.time"):
            handler.capture_selected_text()

        mock_rel.assert_called_once_with(("ctrl",))


# =========================================================================
# wh-review-pattern-fixes.35: verified Ctrl+C in both capture copies
# =========================================================================

def _vpk_router(results):
    """Build a verified_press_keys side effect that answers per chord.

    ``results`` maps a chord tuple to a list of results consumed in
    order (the Ctrl+C chord fires twice on the fallback path)."""
    def fake_vpk(*keys):
        queue = results[keys]
        return queue.pop(0) if len(queue) > 1 else queue[0]
    return fake_vpk


class TestCaptureVerifiedCopy:
    """wh-review-pattern-fixes.35: both capture copies must not be
    fire-and-forget. An undelivered Ctrl+C leaves the sentinel
    unchanged -- indistinguishable from a genuine empty selection --
    and the first copy's false "no selection" would trigger the Ctrl+A
    fallback, making the AI flow capture and replace the ENTIRE field
    instead of the user's selection. A short delivery fails the capture
    closed with copy_failed=True and never enters the fallback."""

    def test_short_first_copy_fails_closed_no_fallback(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value="whatever")
        vpk_calls = []

        def fake_vpk(*keys):
            vpk_calls.append(keys)
            return (False, 1, 4)

        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys", side_effect=fake_vpk), \
             patch(f"{_MOD}._send_modifier_keyups") as mock_rel, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["copy_failed"] is True
        assert result["text"] == ""
        # The Ctrl+A fallback must NOT be entered: the selection state
        # is unknown, not empty.
        assert result["select_all_fallback"] is False
        assert ("ctrl", "a") not in vpk_calls
        # Only the first sentinel write happened.
        assert handler.clipboard._safe_copy.call_count == 1
        # The copy must not ride fire-and-forget press_keys.
        assert call('ctrl', 'c') not in mock_keys.call_args_list
        # The sentinel was never polled: the copy did not deliver.
        handler._poll_clipboard.assert_not_called()
        # A short chord can leave Ctrl held -- release it.
        mock_rel.assert_called_once_with(("ctrl",))

    def test_verified_first_copy_unchanged_sentinel_enters_fallback(
            self, handler):
        """Existing behavior preserved: a VERIFIED copy with an
        unchanged sentinel is a genuine empty selection and the Ctrl+A
        fallback still runs."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(side_effect=[None, "all text"])
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}._send_modifier_keyups") as mock_rel, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        mock_vpk.assert_any_call('ctrl', 'a')
        mock_rel.assert_not_called()
        assert result["copy_failed"] is False
        assert result["select_all_fallback"] is True
        assert result["text"] == "all text"

    def test_short_fallback_copy_reports_copy_failed_and_armed_fallback(
            self, handler):
        """The second copy runs after a VERIFIED Ctrl+A: a short
        delivery must report both copy_failed (fail closed) and the
        armed whole-field selection (the caller owes the collapse)."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value=None)
        vpk_results = {
            ("ctrl", "c"): [(True, 4, 4), (False, 1, 4)],
            ("ctrl", "a"): [(True, 4, 4)],
        }
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys",
                   side_effect=_vpk_router(vpk_results)), \
             patch(f"{_MOD}._send_modifier_keyups") as mock_rel, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["copy_failed"] is True
        assert result["select_all_fallback"] is True
        assert result["text"] == ""
        assert call('ctrl', 'c') not in mock_keys.call_args_list
        mock_rel.assert_called_once_with(("ctrl",))

    def test_successful_direct_capture_reports_copy_ok(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value="selected text")
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["copy_failed"] is False
        assert result["text"] == "selected text"


# =========================================================================
# wh-review-pattern-fixes.37 / .38: explicit capture-failure signal
# =========================================================================

class TestCaptureFailureSignal:
    """wh-review-pattern-fixes.38: every exit where the capture could
    not determine the selection state must say so.

    Before the fix, four exits returned text='', flush_failed=False and
    copy_failed=False -- byte-identical to a genuine empty selection --
    so the Logic side spoke the ordinary no-text message although the
    Input process had failed. ``capture_failed`` is the explicit
    signal, and it is True on EVERY failed exit, including the two that
    already carry a more specific flag.
    """

    def test_genuine_empty_selection_reports_no_capture_failure(
            self, handler):
        """The one shape that must stay unchanged: nothing was selected
        and nothing failed."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(side_effect=[None, None])
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is False
        assert result["text"] == ""
        assert result["select_all_fallback"] is True

    def test_successful_capture_reports_no_capture_failure(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value="selected text")
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is False

    def test_initial_sentinel_write_failure_reports_capture_failed(
            self, handler):
        """The first _safe_copy failed: no Ctrl+C was ever sent, so
        whether text is selected is unknown."""
        handler.clipboard._safe_copy.return_value = False
        handler._poll_clipboard = Mock()
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys") as mock_vpk, \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is True
        assert result["text"] == ""
        assert result["select_all_fallback"] is False
        mock_keys.assert_not_called()
        mock_vpk.assert_not_called()

    def test_short_ctrl_a_reports_capture_failed(self, handler):
        """The Ctrl+A fallback short-delivered: the field was not
        selected and nothing could be captured."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value=None)
        vpk_results = {("ctrl", "c"): [(True, 4, 4)],
                       ("ctrl", "a"): [(False, 0, 4)]}
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   side_effect=_vpk_router(vpk_results)), \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is True
        assert result["text"] == ""
        # The Ctrl+A did not land, so no collapse is owed.
        assert result["select_all_fallback"] is False

    def test_second_sentinel_failure_reports_capture_failed_and_fallback(
            self, handler):
        """The Ctrl+A verifiably landed before the second sentinel
        write failed: report the failure AND keep the armed selection
        so the caller still collapses it."""
        handler.clipboard._safe_copy.side_effect = [True, False]
        handler._poll_clipboard = Mock(return_value=None)
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is True
        assert result["select_all_fallback"] is True
        assert result["text"] == ""

    def test_exception_path_reports_capture_failed(self, handler):
        """The catch-all swallows the exception and returns the same
        empty shape; it must not look like an empty selection."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(side_effect=RuntimeError("boom"))
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is True
        assert result["text"] == ""

    def test_exception_after_ctrl_a_keeps_the_armed_fallback(self, handler):
        """An exception raised after a verified Ctrl+A must still report
        the armed whole-field selection alongside the failure."""
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(
            side_effect=[None, RuntimeError("boom")])
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["capture_failed"] is True
        assert result["select_all_fallback"] is True

    def test_flush_failure_also_reports_capture_failed(self, handler):
        """capture_failed is the catch-all: it is True on the exits that
        already carry a specific flag too, so no caller can miss one."""
        handler._letter_buffer = ["a"]
        handler._flush_letter_buffer = Mock(return_value=False)
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys"), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["flush_failed"] is True
        assert result["capture_failed"] is True

    def test_copy_failure_also_reports_capture_failed(self, handler):
        handler.clipboard._safe_copy.return_value = True
        handler._poll_clipboard = Mock(return_value="whatever")
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys",
                   return_value=(False, 1, 4)), \
             patch(f"{_MOD}._send_modifier_keyups"), \
             patch(f"{_MOD}.time"):
            result = handler.capture_selected_text()

        assert result["copy_failed"] is True
        assert result["capture_failed"] is True


# =========================================================================
# wh-review-pattern-fixes.32 (a): verified Ctrl+V in replace
# =========================================================================

class TestReplaceVerifiedPaste:
    """replace_selected_text must not report success on the strength of
    an unverified fire-and-forget Ctrl+V. press_keys returns None and
    only logs a short SendInput; a dropped Ctrl+V would leave the
    captured selection on screen while Logic speaks Done."""

    def test_short_ctrl_v_send_returns_failure(self, handler):
        handler.clipboard._safe_copy.return_value = True
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(False, 1, 4)), \
             patch(f"{_MOD}._send_modifier_keyups", create=True) as mock_rel, \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            result = handler.replace_selected_text(
                text="corrected",
                **_arm_capture_target(handler),
            )

        assert result["success"] is False
        # A short chord can leave Ctrl held; it must be released.
        mock_rel.assert_called_once_with(("ctrl",))

    def test_verified_ctrl_v_returns_success(self, handler):
        handler.clipboard._safe_copy.return_value = True
        with patch(f"{_MOD}.press_keys") as mock_keys, \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 4, 4)) as mock_vpk, \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            result = handler.replace_selected_text(
                text="corrected",
                **_arm_capture_target(handler),
            )

        assert result["success"] is True
        mock_vpk.assert_called_once_with('ctrl', 'v')
        # The paste must not go through fire-and-forget press_keys.
        assert call('ctrl', 'v') not in mock_keys.call_args_list


# =========================================================================
# wh-review-pattern-fixes.32 (c): acknowledged verified key press
# =========================================================================

class TestPressKeyVerified:
    """press_key_verified is the acknowledged variant of
    press_key_action for state-changing presses the Logic side must not
    treat as fire-and-forget (the AI transform's fallback-selection
    collapse). It returns {"success": bool} for the request/response
    channel, exactly like capture/replace."""

    def test_verified_press_reports_success(self, handler):
        with patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 2, 2)) as mock_vpk:
            result = handler.press_key_verified(key="right", repeat=1)

        assert result == {"success": True}
        mock_vpk.assert_called_once_with("right")

    def test_short_press_reports_failure(self, handler):
        with patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(False, 1, 2)), \
             patch(f"{_MOD}._send_modifier_keyups", create=True) as mock_rel:
            result = handler.press_key_verified(key="right", repeat=1)

        assert result == {"success": False}
        mock_rel.assert_called_once_with(("right",))

    def test_press_invalidates_shadow_buffer(self, handler):
        with patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 2, 2)):
            handler.press_key_verified(key="right")

        handler.buffer_manager.invalidate.assert_called()

    def test_exception_reports_failure(self, handler):
        with patch(f"{_MOD}.verified_press_keys", create=True,
                   side_effect=RuntimeError("SendInput exploded")):
            result = handler.press_key_verified(key="right")

        assert result == {"success": False}


# =========================================================================
# wh-review-pattern-fixes.33: deferred, ownership-checked restore
# =========================================================================

class TestReplaceDeferredClipboardRestore:
    """replace_selected_text must not restore the user's old clipboard
    on the fixed ~50 ms clipboard_context schedule. A slow target that
    consumes the queued Ctrl+V after that window pastes the OLD
    clipboard over the selection while the flow reports success -- the
    same race already fixed for raw_insert_text (wh-qoyk9), which
    delegates restoration to the utterance-level ownership-checked
    deferred path (UtteranceClipboardManager.mark_clipboard_dirty with
    the post-write seq)."""

    def test_no_synchronous_clipboard_context(self, handler):
        """Regression guard: replace_selected_text must NOT enter
        clipboard_context; the synchronous restore was the race."""
        handler.clipboard._safe_copy.return_value = True
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.clipboard_context") as mock_cc, \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            result = handler.replace_selected_text(
                text="corrected",
                **_arm_capture_target(handler),
            )

        mock_cc.assert_not_called()
        assert result["success"] is True

    def test_marks_write_for_deferred_restore_with_write_seq(self, handler):
        """The corrected-text write is marked for the utterance manager
        with the post-write seq so the deferred restore owns it."""
        def fake_safe_copy(text):
            handler.clipboard.last_clipboard_write_seq = 4242
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            result = handler.replace_selected_text(
                text="corrected",
                **_arm_capture_target(handler),
            )

        handler.utterance_manager.mark_clipboard_dirty.assert_called_once_with(
            write_seq=4242
        )
        assert result["success"] is True

    def test_failed_clipboard_write_does_not_mark_dirty(self, handler):
        """A failed corrected-text write leaves the clipboard untouched;
        no deferred restore is owed."""
        handler.clipboard._safe_copy.return_value = False
        with patch(f"{_MOD}.press_keys"), \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   return_value=(True, 4, 4)), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            result = handler.replace_selected_text(
                text="corrected",
                **_arm_capture_target(handler),
            )

        assert result["success"] is False
        handler.utterance_manager.mark_clipboard_dirty.assert_not_called()

    def test_slow_target_gets_correction_not_prior_clipboard(self, handler):
        """Slow-target regression: the target consumes the queued Ctrl+V
        only after replace_selected_text returned -- i.e. after the old
        fixed restore schedule has already run. It must receive the
        correction, never the prior clipboard."""
        simulated_clipboard = {"value": "OLD_USER_CLIPBOARD"}

        def fake_safe_copy(text):
            simulated_clipboard["value"] = text
            handler.clipboard.last_clipboard_write_seq = 4242
            return True

        handler.clipboard._safe_copy.side_effect = fake_safe_copy

        class RestoringContext:
            """Mimics production clipboard_context: saves at enter,
            synchronously restores at exit after the fixed delay. If
            replace_selected_text still uses it, the old clipboard is
            back before a slow target consumes Ctrl+V."""

            def __init__(self, *args, **kwargs):
                self._saved = None

            def __enter__(self):
                self._saved = simulated_clipboard["value"]
                return self

            def __exit__(self, *exc):
                simulated_clipboard["value"] = self._saved
                return False

        sent = []
        with patch(f"{_MOD}.press_keys",
                   side_effect=lambda *k: sent.append(k)), \
             patch(f"{_MOD}.verified_press_keys", create=True,
                   side_effect=lambda *k: (sent.append(k), (True, 4, 4))[1]), \
             patch(f"{_MOD}.clipboard_context", RestoringContext), \
             patch(f"{_MOD}.time") as mock_time:
            mock_time.sleep = Mock()
            result = handler.replace_selected_text(
                text="CORRECTED_TEXT",
                **_arm_capture_target(handler),
            )

        assert result["success"] is True
        assert ("ctrl", "v") in sent
        # Consume time: after return. The correction must still be there.
        assert simulated_clipboard["value"] == "CORRECTED_TEXT"
