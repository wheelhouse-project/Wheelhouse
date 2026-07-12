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
    """
    with patch(f"{_MOD}.TextPerfector"), \
         patch(f"{_MOD}.ClipboardOperations"), \
         patch(f"{_MOD}.WindowFocusManager"), \
         patch(f"{_MOD}.SelectionTransformer"), \
         patch(f"{_MOD}.UtteranceClipboardManager"), \
         patch(f"{_MOD}.ShadowBufferManager"), \
         patch(f"{_MOD}.TerminalEditorProxy"), \
         patch(f"{_MOD}.InsertionRouter"):

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
        handler.rejection_text_cache.put(token, "the original text")

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

    def test_hit_response_carries_verified_outcome(self, handler):
        token = _new_token()
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello world")
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
        handler.rejection_text_cache.put(token, "hello world")
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
        handler.rejection_text_cache.put(token, "hello world")
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
        handler.rejection_text_cache.put(token, "hello world")
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
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello")
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
        handler.rejection_text_cache.put(token, "hello", target_hwnd=0x12345)

        call_log: list[str] = []
        handler.window_manager.ensure_focused.side_effect = (
            lambda hwnd: call_log.append(f"refocus({hwnd:#x})") or True
        )

        with patch(f"{_MOD}.capture_context") as mock_capture:
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

    def test_hit_with_zero_hwnd_skips_refocus(self, handler):
        """When the cache entry has target_hwnd=0 (legacy or stale-COM
        rejection), the retry handler must NOT call ensure_focused.
        Calling ensure_focused(0) would no-op anyway but the contract
        is to skip the call entirely so the win32 layer is not touched.
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
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process:
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
        )

        with patch(f"{_MOD}.capture_context") as mock_capture, \
             patch(f"{_MOD}.win32process") as mock_win32process:
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
        handler.rejection_text_cache.put(token, "hello", target_hwnd=0xDEAD)
        handler.window_manager.ensure_focused.return_value = False

        with patch(f"{_MOD}.capture_context") as mock_capture:
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
        handler.rejection_text_cache.put(token, self.SECRET)

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
        handler.rejection_text_cache.put(token, self.SECRET)

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
        handler.rejection_text_cache.put(token, "hello")

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
        handler.rejection_text_cache.put(token, "hello")

        with patch(f"{_MOD}.capture_context") as mock_capture:
            mock_capture.return_value = MagicMock(focused_control=MagicMock())
            handler.retry_dictation_by_token(
                correlation_token=token,
                override_strategy=OVERRIDE_CLIPBOARD_ONLY,
                request_id="req-abc",
            )

        msg = handler.response_queue.put.call_args[0][0]
        assert msg.get("action") == "retry_dictation_by_token"
