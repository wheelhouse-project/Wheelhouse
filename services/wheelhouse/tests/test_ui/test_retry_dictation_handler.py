"""Tests for the input-process retry_dictation_by_token handler (wh-ftg63).

When the user clicks "Try it anyway" on a rejection toast (Phase 4 of
wh-9weum), Logic forwards a ``retry_dictation_by_token`` request with
the correlation_token and ``override_strategy='clipboard_only'``. The
input process resolves the token in the rejection-text cache and:

  * cache HIT  -> dispatches the cached text through ClipboardOnlyStrategy
                  and returns ``status='success'`` with the strategy's
                  ``retry_outcome`` ('verified' or 'unverified').
  * cache MISS -> returns ``status='unknown_token'``. No counter.
  * cache EXPIRED -> returns ``status='token_expired'``. No counter.

Privacy contract (wh-x4mv.2 round 2): the dictation text leaves Input
only as the clipboard write performed by ClipboardOnlyStrategy. The
text MUST NOT appear in any IPC payload, GUI message, or log line.

Coverage:
  * Cache HIT path runs ClipboardOnlyStrategy and returns success
    with the strategy's retry_outcome.
  * Cache MISS returns ``unknown_token`` with no retry_outcome.
  * Cache EXPIRED returns ``token_expired`` with no retry_outcome.
  * Privacy: response payload never carries the cached text.
  * Privacy: log records produced by the handler never carry the
    cached text.
  * Malformed request payload -> handler returns ``unknown_token``
    response (graceful degrade per wh-uf54).
  * The override path bypasses the normal router decision -- the
    handler invokes ClipboardOnlyStrategy directly even if the
    router would have picked a different strategy.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest

from services.wheelhouse.shared.retry_dictation_by_token import (
    OVERRIDE_CLIPBOARD_ONLY,
    RetryDictationByTokenResponse,
    STATUS_SUCCESS,
    STATUS_TOKEN_EXPIRED,
    STATUS_UNKNOWN_TOKEN,
)
from ui.context import UIContext
from ui.hwnd_utils import top_level_hwnd_from_control
from ui.rejection_text_cache import (
    CacheResult,
    CacheStatus,
    RejectionTextCache,
)
from ui.strategies.base import InsertionResult


_TOKEN_FIXED = "11111111-1111-4111-8111-111111111111"


def _new_token() -> str:
    return str(uuid.uuid4())


_MOD = "ui.ui_action_handler"


@pytest.fixture
def handler():
    """Build a UIActionHandler with all specialist components mocked.

    Mirrors the fixture in test_ui_action_handler.py. Strategy classes
    are NOT patched because the production code uses isinstance()
    checks against them.

    normalize_hwnd_for_foreground_compare defaults to returning
    0x12345 -- the canonical test HWND -- so HIT-path tests whose cache
    entry carries target_hwnd=0x12345/target_root=0x12345 pass the
    root gates without each repeating the patch. Tests that exercise
    root drift or normalization failure patch it locally; the inner
    patch wins inside its ``with`` block.

    read_hwnd_provenance defaults to returning 7 -- the canonical test
    provenance marker -- so HIT-path entries carrying target_tag=7
    pass the .1.12 tag gates the same way. Tag-mismatch tests patch it
    locally.

    top_level_hwnd_from_control defaults to returning 0x12345 so the
    .1.16 capture-binding gate resolves the captured control back to
    the canonical target window. Capture-drift tests patch it locally.
    """
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"), \
         patch(
             f"{_MOD}.normalize_hwnd_for_foreground_compare",
             return_value=0x12345,
         ), \
         patch(
             f"{_MOD}.read_hwnd_provenance",
             return_value=7,
         ), \
         patch(
             f"{_MOD}.top_level_hwnd_from_control",
             return_value=0x12345,
         ):

        from ui.ui_action_handler import UIActionHandler

        q = MagicMock()
        h = UIActionHandler(response_queue=q, config={"ui_actions": {}})
        h.terminal_editor.is_active = False

        # Substitute the live RejectionTextCache (real object so we can
        # exercise resolve()'s three-way outcome) and a stubbed
        # ClipboardOnlyStrategy whose insert is observable.
        h.rejection_text_cache = RejectionTextCache()
        h.clipboard_only_strategy = MagicMock()
        h.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )
        h.clipboard_only_strategy.reset_preceding_mirror = MagicMock()

        yield h


# ---------------------------------------------------------------------------
# Cache HIT path
# ---------------------------------------------------------------------------


class TestCacheHit:
    def test_hit_runs_clipboard_only_strategy(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "the original text",
            target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_called_once()
        # First positional arg is the cached text.
        args, _kwargs = handler.clipboard_only_strategy.insert.call_args
        assert args[0] == "the original text"

    def test_hit_passes_cached_identity_to_strategy(self, handler):
        # wh-ensure-focused-same-process-fallback.1.18 (codex round
        # 11): every handler-level provenance probe runs BEFORE
        # ClipboardOnlyStrategy re-resolves its paste target from the
        # captured control, and the clipboard verify loop inside
        # verified_paste leaves a real interval before the Ctrl+V. The
        # handler must hand the verified cached identity (tagged
        # window, marker) down to the strategy so the paste path can
        # re-read the marker immediately before the keystroke.
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "the original text",
            target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_called_once()
        _args, kwargs = handler.clipboard_only_strategy.insert.call_args
        assert kwargs.get("retry_identity") == (0x12345, 7)

    def test_hit_response_carries_verified_outcome(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        msg = handler.response_queue.put.call_args[0][0]
        # Must parse as a valid response per the IPC contract.
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_SUCCESS
        assert parsed.retry_outcome == "verified"

    def test_hit_verified_outcome_invalidates_cache_entry(self, handler):
        # wh-override-multiword-retry finding 1: a verified Try-it-anyway
        # click must drop the cache entry so the next stretch of
        # dictation against the same target gets a fresh correlation
        # token instead of being aggregated onto the now-consumed entry.
        # Logic adds the token to consumed_retry_tokens on the same
        # verified outcome; if Input kept appending, the user's next
        # click would be silently short-circuited by the duplicate
        # check.
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello world", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        assert handler.rejection_text_cache.resolve(token).status is CacheStatus.MISS

    def test_hit_verified_outcome_calls_forget_token_on_strategy(self, handler):
        # wh-override-multiword-retry.2.2 (deepseek finding): the retry
        # handler must also tell the rejected strategy to drop any
        # aggregation bucket pointing at the now-consumed token, so the
        # bucket map stays synchronised with the cache without waiting
        # for the next emission's prune.
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello world", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="verified",
        )
        # Replace rejected_strategy with a tracking mock for this test.
        handler.rejected_strategy = MagicMock()

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.rejected_strategy.forget_token.assert_called_once_with(token)

    def test_hit_unverified_outcome_does_not_call_forget_token(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello world", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="unverified",
        )
        handler.rejected_strategy = MagicMock()

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.rejected_strategy.forget_token.assert_not_called()

    def test_hit_unverified_outcome_keeps_cache_entry(self, handler):
        # Unverified retries leave the cache entry intact so the user
        # can click Try-it-anyway again; Logic does not consume the
        # token on this outcome either.
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello world", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="unverified",
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        result = handler.rejection_text_cache.resolve(token)
        assert result.status is CacheStatus.HIT
        assert result.text == "hello world"

    def test_hit_response_carries_unverified_outcome(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=True, clipboard_dirty=True, retry_outcome="unverified",
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_SUCCESS
        assert parsed.retry_outcome == "unverified"

    def test_hit_strategy_failure_emits_token_expired(self, handler):
        # ClipboardOnlyStrategy returns success=False when it refused
        # before sending Ctrl+V (specific.py:1520). Nothing landed on
        # screen and the user must see the follow-up toast, so the
        # handler emits token_expired with reason='delivery_failed'
        # rather than reporting a misleading success. The logic-side
        # forwarder maps any non-success status to the canonical
        # follow-up wording (wh-vbvgf.1.1).
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.clipboard_only_strategy.insert.return_value = InsertionResult(
            success=False, clipboard_dirty=True, retry_outcome="unverified",
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "delivery_failed"

    def test_hit_bypasses_router(self, handler):
        # The override path must NOT consult the router; ClipboardOnlyStrategy
        # is forced regardless of what context the predicate would have
        # rejected with.
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.router = MagicMock()  # router.get_strategy must not be called

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.router.get_strategy.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_called_once()

    def test_hit_outside_utterance_wraps_clipboard_context(self, handler):
        # The retry click usually fires outside an active utterance, so
        # mark_clipboard_dirty + end_utterance restore is not in scope.
        # The handler must wrap the strategy call in clipboard_context to
        # restore the user's prior clipboard contents (wh-vbvgf.1.2).
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.utterance_manager.is_in_utterance.return_value = False

        with patch(f"{_MOD}.clipboard_context") as mock_ctx, \
             patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        mock_ctx.assert_called_once_with(restore_delay=0.05)
        handler.clipboard_only_strategy.insert.assert_called_once()

    def test_hit_inside_utterance_does_not_wrap_clipboard_context(self, handler):
        # Inside an active utterance, mark_clipboard_dirty + end_utterance
        # already restore the clipboard. A second clipboard_context wrap
        # would double-restore (wh-vbvgf.1.2).
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.utterance_manager.is_in_utterance.return_value = True

        with patch(f"{_MOD}.clipboard_context") as mock_ctx, \
             patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        mock_ctx.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_called_once()

    def test_hit_resets_preceding_mirror_before_each_replay(self, handler):
        """wh-soft-allow-verdict-tier.1.1: each retry replay must reset
        the ClipboardOnlyStrategy's preceding-chars mirror so two
        consecutive clicks on the same toast produce the same perfected
        paste output. Logic leaves the token in the cache after an
        unverified outcome (the keystroke fired but verification could
        not confirm delivery), so the user can click Try-it-anyway
        again; without the reset the second click would perfect
        cached_text against the first click's perfected output and
        produce a different paste."""

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )
        handler.utterance_manager.is_in_utterance.return_value = False

        with patch(f"{_MOD}.clipboard_context"), \
             patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            # First replay (unverified outcome -- token stays in cache).
            handler.clipboard_only_strategy.insert.return_value = (
                InsertionResult(
                    success=True,
                    clipboard_dirty=True,
                    retry_outcome="unverified",
                )
            )
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )
            # Second replay against the same live token.
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-2",
            )

        # The mirror reset must be called before each insert. Two
        # replays must produce two reset calls in the same order as the
        # insert calls.
        reset = handler.clipboard_only_strategy.reset_preceding_mirror
        insert = handler.clipboard_only_strategy.insert
        assert reset.call_count == 2
        assert insert.call_count == 2
        # Mock.mock_calls records every call across attributes on the
        # parent MagicMock, so the relative order of reset_preceding_mirror
        # and insert is observable. Each insert must follow its reset.
        ordered = [
            c[0] for c in handler.clipboard_only_strategy.mock_calls
            if c[0] in {"reset_preceding_mirror", "insert"}
        ]
        assert ordered == [
            "reset_preceding_mirror", "insert",
            "reset_preceding_mirror", "insert",
        ]
        # wh-soft-allow-verdict-tier.2.1: assert the user-visible
        # invariant too. Both replays receive the same cached_text as
        # the first positional arg to insert. This catches a future
        # refactor where the call order is preserved but a different
        # input slips through (e.g. the handler accidentally caches a
        # perfected string between calls).
        first_call_text = insert.call_args_list[0][0][0]
        second_call_text = insert.call_args_list[1][0][0]
        assert first_call_text == "hello"
        assert second_call_text == "hello"

    def test_hit_refocuses_target_hwnd_before_capture(self, handler):
        """wh-override-paste-focus-drift: when the rejection event carried
        the original target's top-level HWND, the retry handler must
        restore foreground to that HWND BEFORE capture_context() runs.
        Otherwise the captured context reflects the toast's QPushButton
        (the click landed on it) and ClipboardOnlyStrategy pastes into
        the toast button, which silently consumes the keystroke.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        call_log: list[str] = []
        handler.window_manager.ensure_focused.side_effect = (
            lambda hwnd: call_log.append(f"refocus({hwnd:#x})") or True
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_normalize.return_value = 0x12345
            def _capture_side_effect():
                call_log.append("capture")
                return MagicMock(focused_control=MagicMock())
            mock_capture.side_effect = _capture_side_effect
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        # ensure_focused must run, and must run before capture_context.
        handler.window_manager.ensure_focused.assert_called_once_with(0x12345)
        assert call_log == ["refocus(0x12345)", "capture"], (
            f"expected refocus before capture, got {call_log}"
        )

    def test_hit_zero_hwnd_refuses_replay(self, handler):
        """wh-ensure-focused-same-process-fallback.1.11: a cache entry
        with target_hwnd=0 (rejection-time HWND lookup failed -- stale
        COM, no top-level) carries NO target identity at all. Every
        identity guard in the handler sits inside an ``if target_hwnd:``
        block, so such an entry used to skip the PID check, the root
        gates, AND the refocus, then paste into whatever window holds
        foreground when the user clicks Try-it-anyway -- the original
        wh-override-paste-focus-drift toast-button failure, resurrected
        for exactly the entries whose target is least known. The
        handler must refuse outright: no refocus, no capture, no
        paste, token_expired so the GUI surfaces the canonical
        follow-up wording.
        """

        token = _new_token()
        handler.rejection_text_cache.put(token, "hello", target_hwnd=0)

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_not_called()
        mock_capture.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_pid_mismatch_emits_token_expired_and_skips_paste(self, handler):
        """wh-override-paste-focus-drift.1.2: when the cached target HWND
        has been reused by Windows for a different process, the retry
        handler must NOT refocus or paste. The dictation text was
        captured against the original process; pasting into a reused
        HWND would deliver it to an unrelated window. Emit token_expired
        so the GUI surfaces the canonical follow-up wording instead.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process:
            # The HWND now belongs to a different process (HWND reuse).
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 9999,
            )
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        # Refocus was NOT attempted; paste was NOT attempted.
        handler.window_manager.ensure_focused.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()

        # Response was token_expired with a reason naming the cause.
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_pid_match_proceeds_with_refocus(self, handler):
        """Happy path: cached PID matches the live HWND's PID. The
        handler calls ensure_focused, then capture_context, then
        ClipboardOnlyStrategy.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_normalize.return_value = 0x12345
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_called_once_with(0x12345)
        handler.clipboard_only_strategy.insert.assert_called_once()

    def test_hit_pid_zero_in_cache_skips_pid_check(self, handler):
        """A cached target_process_id of 0 means 'no PID was recorded at
        rejection time' (legacy path or context.process_id was 0). The
        handler skips the GetWindowThreadProcessId comparison and
        proceeds with the refocus + paste using the cached HWND.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=0,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_normalize.return_value = 0x12345
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        mock_win32process.GetWindowThreadProcessId.assert_not_called()
        handler.window_manager.ensure_focused.assert_called_once_with(0x12345)
        handler.clipboard_only_strategy.insert.assert_called_once()

    def test_hit_get_window_thread_pid_returns_zero_treats_as_gone(self, handler):
        """GetWindowThreadProcessId returns 0 for a destroyed HWND. The
        handler must treat that as 'window gone' and emit token_expired,
        not proceed with a paste against a defunct handle.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process:
            mock_win32process.GetWindowThreadProcessId.return_value = (0, 0)
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_refocus_failure_emits_token_expired(self, handler):
        """If ensure_focused returns False, the handler must NOT paste.

        The GUI process issues an AllowSetForegroundWindow grant before
        forwarding the click IPC (round 2 of wh-override-paste-focus-drift),
        so Input's SetForegroundWindow call succeeds in normal use. A
        False return from ensure_focused after the grant means the
        target is genuinely unreachable -- closed, minimized, or hidden
        in a way Windows refuses to override even with the grant.
        Pasting anyway would send Ctrl+V to whatever holds foreground
        at that moment, leaking the cached dictation into an unrelated
        control. Fail closed with token_expired so the GUI surfaces the
        canonical follow-up wording. See wh-override-retry-fail-open-leak.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0xDEAD, target_root=0xDEAD,
            target_tag=7,
        )
        handler.window_manager.ensure_focused.return_value = False

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_normalize.return_value = 0xDEAD
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_called_once_with(0xDEAD)
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_root_drift_emits_token_expired_and_skips_paste(self, handler):
        """wh-ensure-focused-same-process-fallback.1.7: the PID guard alone
        cannot see a SAME-process handle recycle. The rejected Brave
        helper HWND H was its own GA_ROOT at rejection time; H's window
        closes and Windows reuses H for a CHILD of another Brave
        top-level B in the same browser process. The live PID still
        matches, and ensure_focused's normalized strict compare maps H
        to B and credits it -- the paste would land in B, a window the
        user never dictated into. The handler must compare the live
        GA_ROOT of H against the rejection-time snapshot and fail
        closed on drift, before ensure_focused runs.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            # H now normalizes to a DIFFERENT root: Windows recycled it
            # as a child of another same-process top-level window.
            mock_normalize.return_value = 0x22222
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        mock_normalize.assert_called_once_with(0x12345)
        handler.window_manager.ensure_focused.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_root_unresolvable_emits_token_expired(self, handler):
        """When a root snapshot exists but the live normalization fails
        (GetAncestor error, destroyed handle), the handler cannot prove
        the handle still names the rejection-time window. Fail closed
        with token_expired rather than refocus a handle of unknown
        identity.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_normalize.return_value = None
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_root_match_proceeds_with_refocus(self, handler):
        """Happy path for the root guard: the live GA_ROOT of the cached
        HWND equals the rejection-time snapshot (the invisible Brave
        helper is still alive and still its own root). The handler
        proceeds to ensure_focused and the paste -- the guard must not
        break the same-process fallback it protects.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_normalize.return_value = 0x12345
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_called_once_with(0x12345)
        handler.clipboard_only_strategy.insert.assert_called_once()

    def test_hit_root_drift_during_refocus_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.9: the pre-refocus
        root check alone cannot cover the refocus interval. The first
        normalization sees the live helper H and matches the snapshot;
        Windows recycles H as a child of foreground sibling B during
        ensure_focused (whose strict normalized compare then credits
        B); a handler that captures and pastes on that credit sends
        the text into B. The handler must re-normalize the handle
        AFTER a successful ensure_focused, immediately before
        capture_context, and refuse on drift.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            # First (pre-refocus) call sees the live helper; the
            # second (post-refocus) call sees the recycled handle's
            # new root.
            mock_normalize.side_effect = [0x12345, 0x22222]
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        assert mock_normalize.call_count == 2
        handler.window_manager.ensure_focused.assert_called_once_with(0x12345)
        mock_capture.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_root_unresolvable_after_refocus_refuses(self, handler):
        """When the post-refocus normalization fails (handle destroyed
        mid-refocus), the handler cannot prove the handle still names
        the rejection-time window. Refuse by default rather than
        capture and paste against a handle of unknown identity.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_normalize.side_effect = [0x12345, None]
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        mock_capture.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_missing_root_snapshot_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.8: a cached entry
        with a nonzero HWND but target_root=0 (rejection-time GA_ROOT
        normalization failed) must refuse, not skip the root check.
        The cache lives only in Input-process memory, so no persisted
        legacy entry needs a skip path -- and skipping recreates the
        .1.7 wrong-window sequence for exactly the entries whose
        rejection-time identity is least known. This IS the same-PID
        child-reuse regression: the live PID still matches, and only
        the missing snapshot separates this replay from .1.7's.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare"
             ) as mock_normalize:
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        mock_normalize.assert_not_called()
        handler.window_manager.ensure_focused.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_missing_provenance_tag_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.12: a cached entry
        with a nonzero HWND but target_tag=0 (rejection-time SetProp
        failed, or a legacy caller omitted it) must refuse, not skip
        the tag check -- the same refuse-outright contract as the
        .1.8 root gate, for the same reason: skipping recreates the
        recycle exposure for exactly the entries whose rejection-time
        identity is least known.

        read_hwnd_provenance is patched to 0 here so the live window
        also reads untagged: 0 == 0 must never count as a match, and
        only the zero-tag refusal separates this replay from a paste.
        (With the fixture default of 7, the mismatch check downstream
        would mask a dropped zero-tag gate -- the mutation-gate M42
        false pass, 2026-08-23.)
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.read_hwnd_provenance",
                 return_value=0,
             ):
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_not_called()
        mock_capture.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_provenance_tag_mismatch_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.12: the live
        window's property no longer carries the rejection-time marker.
        This is the recycled-as-own-root case every handle-value guard
        aliases: Windows reused the numeric handle for a NEW same-PID
        top-level window that is its own GA_ROOT, so normalize returns
        the handle itself (matching the snapshot), the PID matches,
        and ensure_focused's strict compare credits it. Only the
        window-OBJECT property tells the two windows apart -- the new
        object never carried it, so the read returns 0.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.read_hwnd_provenance",
                 return_value=0,
             ):
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_not_called()
        mock_capture.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_provenance_tag_lost_during_refocus_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.12: the pre-check
        cannot cover the refocus interval -- the window can be
        destroyed and its handle recycled DURING ensure_focused, after
        every earlier probe passed. Re-read the property after the
        successful refocus, immediately before capture_context, and
        refuse when the marker is gone (the recycled object reads 0).
        The first read (pre-check) sees the marker; the second
        (post-refocus) does not.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_process_id=4242,
            target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process, \
             patch(
                 f"{_MOD}.read_hwnd_provenance",
                 side_effect=[7, 0],
             ):
            mock_win32process.GetWindowThreadProcessId.return_value = (
                0, 4242,
            )
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.window_manager.ensure_focused.assert_called_once_with(0x12345)
        mock_capture.assert_not_called()
        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_capture_resolves_to_different_window_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.16 (codex round
        10): every probe above capture_context validates the CACHED
        handle, but ClipboardOnlyStrategy derives its paste target
        from the freshly captured control. A focus change during
        capture_context (UIA focus resolution, psutil, top-level walk
        -- a real interval) hands the strategy a different window and
        the cached text pastes there. The handler must bind the
        captured control back to the verified target root and refuse
        on mismatch.

        read_hwnd_provenance stays at the fixture default (7 for any
        handle) on purpose: the root comparison must be the only gate
        separating refusal from a paste, so the mutation gate can
        prove the root comparison is load-bearing.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        def _normalize(hwnd):
            return {0x12345: 0x12345, 0xBEEF: 0xBEEF}[hwnd]

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(
                 f"{_MOD}.top_level_hwnd_from_control",
                 return_value=0xBEEF,
             ), \
             patch(
                 f"{_MOD}.normalize_hwnd_for_foreground_compare",
                 side_effect=_normalize,
             ):
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_capture_returns_no_resolvable_control_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.16: when the
        captured control cannot be resolved to a top-level handle
        (no focused control, stale COM, zero NativeWindowHandle),
        the handler cannot prove the capture landed on the verified
        target. Fail closed -- pasting anyway would deliver into
        whatever capture_context happened to return.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(
                 f"{_MOD}.top_level_hwnd_from_control",
                 return_value=None,
             ):
            mock_capture.return_value = MagicMock(focused_control=None)
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"

    def test_hit_capture_tag_gone_after_capture_refuses(self, handler):
        """wh-ensure-focused-same-process-fallback.1.16: the captured
        top-level resolves to the same root by handle value, but the
        provenance marker is gone -- the window object was recycled
        during capture_context and the recycled handle normalizes to
        itself. The root comparison alone cannot see this (handle
        values alias); the property re-read on the captured top-level
        is the probe a SAME-RUN recycle cannot alias (cross-run,
        equal 43-bit salts collide at about 2**-43 per pair of runs
        -- the accepted residual at _RUN_SALT). Reads: pre-refocus 7,
        post-refocus 7, post-capture 0.
        """

        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(
                 f"{_MOD}.read_hwnd_provenance",
                 side_effect=[7, 7, 0],
             ):
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"


# ---------------------------------------------------------------------------
# Cache MISS path (token never seen)
# ---------------------------------------------------------------------------


class TestCacheMiss:
    def test_unknown_token_returns_unknown_token_status(self, handler):
        token = _new_token()
        # Cache deliberately empty.

        handler.retry_dictation_by_token(
            correlation_token=token,
            override_strategy=OVERRIDE_CLIPBOARD_ONLY,
            request_id="req-1",
        )

        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_UNKNOWN_TOKEN
        assert parsed.retry_outcome is None

    def test_unknown_token_does_not_run_strategy(self, handler):
        token = _new_token()

        handler.retry_dictation_by_token(
            correlation_token=token,
            override_strategy=OVERRIDE_CLIPBOARD_ONLY,
            request_id="req-1",
        )

        handler.clipboard_only_strategy.insert.assert_not_called()


# ---------------------------------------------------------------------------
# Cache EXPIRED path (TTL elapsed)
# ---------------------------------------------------------------------------


class TestCacheExpired:
    def test_expired_token_returns_token_expired_status(self, handler):
        # Inject a fake clock so we can age the entry past the TTL.
        clock = {"now": 1000.0}

        def _time_source():
            return clock["now"]

        handler.rejection_text_cache = RejectionTextCache(
            ttl_seconds=10.0, time_source=_time_source,
        )
        token = _new_token()
        handler.rejection_text_cache.put(token, "stale text")

        # Advance past TTL.
        clock["now"] = 1100.0

        handler.retry_dictation_by_token(
            correlation_token=token,
            override_strategy=OVERRIDE_CLIPBOARD_ONLY,
            request_id="req-1",
        )

        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.retry_outcome is None

    def test_expired_token_does_not_run_strategy(self, handler):
        clock = {"now": 1000.0}

        def _time_source():
            return clock["now"]

        handler.rejection_text_cache = RejectionTextCache(
            ttl_seconds=10.0, time_source=_time_source,
        )
        token = _new_token()
        handler.rejection_text_cache.put(token, "stale text")
        clock["now"] = 1100.0

        handler.retry_dictation_by_token(
            correlation_token=token,
            override_strategy=OVERRIDE_CLIPBOARD_ONLY,
            request_id="req-1",
        )

        handler.clipboard_only_strategy.insert.assert_not_called()


# ---------------------------------------------------------------------------
# Privacy property (wh-x4mv.2 round 2)
# ---------------------------------------------------------------------------


class TestPrivacy:
    SECRET = "this is the secret dictated text never log me"

    def test_response_payload_does_not_contain_cached_text(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, self.SECRET, target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        # Inspect every value the handler put on the response queue.
        for call in handler.response_queue.put.call_args_list:
            msg = call.args[0]
            for value in msg.values():
                assert self.SECRET not in str(value), (
                    "cached text leaked into response payload: "
                    f"value={value!r}"
                )

    def test_log_lines_do_not_contain_cached_text(self, handler, caplog):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, self.SECRET, target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            with caplog.at_level(logging.DEBUG):
                handler.retry_dictation_by_token(
                    correlation_token=token,
                    override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                    request_id="req-1",
                )

        for record in caplog.records:
            # message + args formatting
            assert self.SECRET not in record.getMessage(), (
                "cached text leaked into log line: %r" % record.getMessage()
            )

    def test_log_lines_do_not_contain_cached_text_on_miss(self, handler, caplog):
        # Even when the strategy is NOT run (cache miss / expired), the
        # handler may log the correlation_token and outcome. The cached
        # text is unavailable on miss but the token must not be confused
        # with the text in any log assertion.
        token = _new_token()
        # Cache empty: simulate that some other token's text exists but
        # ours does not -- the SECRET should not appear because the
        # handler should not access other entries.
        handler.rejection_text_cache.put(_new_token(), self.SECRET)

        with caplog.at_level(logging.DEBUG):
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        for record in caplog.records:
            assert self.SECRET not in record.getMessage()


# ---------------------------------------------------------------------------
# Malformed request handling (graceful degrade per wh-uf54)
# ---------------------------------------------------------------------------


class TestMalformedRequest:
    def test_invalid_correlation_token_returns_unknown_token(self, handler):
        # Not a uuid4: schema validation rejects, handler degrades to
        # unknown_token (no exception out, no strategy run).
        handler.retry_dictation_by_token(
            correlation_token="not-a-valid-uuid",
            override_strategy=OVERRIDE_CLIPBOARD_ONLY,
            request_id="req-1",
        )

        # Either no response (drop) OR an unknown_token response. Both
        # are acceptable graceful degrades; assert the strategy did not
        # run and no exception escaped.
        handler.clipboard_only_strategy.insert.assert_not_called()
        # If a response was sent, it must NOT have status='success'.
        if handler.response_queue.put.call_args_list:
            msg = handler.response_queue.put.call_args[0][0]
            parsed = RetryDictationByTokenResponse.from_dict(msg)
            assert parsed.status != STATUS_SUCCESS

    def test_invalid_override_strategy_returns_non_success(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(token, "x")

        handler.retry_dictation_by_token(
            correlation_token=token,
            override_strategy="totally_unknown_strategy",
            request_id="req-1",
        )

        handler.clipboard_only_strategy.insert.assert_not_called()
        if handler.response_queue.put.call_args_list:
            msg = handler.response_queue.put.call_args[0][0]
            parsed = RetryDictationByTokenResponse.from_dict(msg)
            assert parsed.status != STATUS_SUCCESS


# ---------------------------------------------------------------------------
# Response carries action and request_id so the demuxer can resolve the Future
# ---------------------------------------------------------------------------


class TestResponseEnvelope:
    def test_response_carries_request_id(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-abc",
            )

        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("request_id") == "req-abc"

    def test_response_carries_action_name(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello", target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-abc",
            )

        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("action") == "retry_dictation_by_token"


# ---------------------------------------------------------------------------
# Focused-control read failure during the retry capture
# ---------------------------------------------------------------------------


class TestFocusReadFailure:
    """Pin the refusal reason when capture_context reports a failed read.

    wh-insert-focus-read-stall changed ``ui.context.capture_context`` so a
    focused-control read that raises returns a UIContext carrying
    ``focused_control=None`` and ``focus_read_failed=True``, instead of
    letting the COM error escape. Before that change the error reached this
    function's own outer ``except`` block (the "ClipboardOnlyStrategy raised"
    handler) and the emitted token_expired carried
    ``reason="strategy_error"``. Now the .1.16 capture-binding gate meets a
    control it cannot resolve to a top-level handle, so ``captured_root`` is
    None and the emitted reason is ``"target_window_gone"``. The boss
    accepted the new reason string on 2026-09-18; this test pins it so any
    later change to it is deliberate.
    """

    def test_failed_focus_read_emits_target_window_gone(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(
            token, "hello",
            target_hwnd=0x12345, target_root=0x12345,
            target_tag=7,
        )

        failed_read_context = UIContext(
            focused_control=None,
            is_flutter=False,
            is_terminal=False,
            process_name="",
            class_name="",
            focus_read_failed=True,
        )

        # The fixture stubs top_level_hwnd_from_control to return the
        # canonical handle for any argument. Restore the real resolver here
        # so the absent control resolves the way production resolves it
        # (no control -> None).
        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(
                 f"{_MOD}.top_level_hwnd_from_control",
                 side_effect=top_level_hwnd_from_control,
             ):
            mock_capture.return_value = failed_read_context
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-1",
            )

        handler.clipboard_only_strategy.insert.assert_not_called()
        msg = handler.response_queue.put.call_args[0][0]
        parsed = RetryDictationByTokenResponse.from_dict(msg)
        assert parsed.status == STATUS_TOKEN_EXPIRED
        assert parsed.reason == "target_window_gone"
