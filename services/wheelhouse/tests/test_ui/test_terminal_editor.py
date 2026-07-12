"""Tests for TerminalEditorProxy (IPC proxy for Qt editor in GUI Process).

wh-1g6er: the slim proxy that survived the terminal-editor strategy
deletion. The focus-redirect path opens an empty editor and drain
words flow through Standard / VerifiedUnicode against the editor's
QPlainTextEdit directly. The proxy is responsible only for opening /
cancelling the editor and consuming submit-lifecycle acks; the
stale-event tracking and ack-driven retract counter advancement that
the legacy strategy needed have been removed.
"""
import time
from unittest.mock import MagicMock
import pytest


@pytest.fixture
def proxy():
    from ui.terminal_editor_proxy import TerminalEditorProxy
    response_queue = MagicMock()
    clipboard_ops = MagicMock(accumulated_paste_chars=0)
    p = TerminalEditorProxy(response_queue=response_queue, clipboard_ops=clipboard_ops)
    return p, response_queue


@pytest.fixture
def proxy_with_clipboard():
    """Variant exposing the clipboard_ops mock for assertions."""
    from ui.terminal_editor_proxy import TerminalEditorProxy
    response_queue = MagicMock()
    clipboard_ops = MagicMock(accumulated_paste_chars=0)
    p = TerminalEditorProxy(response_queue=response_queue, clipboard_ops=clipboard_ops)
    return p, response_queue, clipboard_ops


class TestProxyLifecycle:
    def test_not_active_initially(self, proxy):
        p, _ = proxy
        assert not p.is_active

    def test_start_is_noop(self, proxy):
        p, _ = proxy
        p.start()  # Should not raise

    def test_stop_is_noop(self, proxy):
        p, _ = proxy
        p.stop()  # Should not raise


class TestProxyShow:
    def test_show_sends_event(self, proxy):
        p, queue = proxy
        p.show("hello", terminal_hwnd=123, geometry=(0, 0, 1920, 1080))
        queue.put.assert_called_once()
        msg = queue.put.call_args[0][0]
        assert msg["type"] == "te_event"
        assert msg["event"] == "show"
        assert msg["text"] == "hello"
        assert msg["hwnd"] == 123
        assert msg["rect"] == (0, 0, 1920, 1080)
        assert "request_id" in msg
        assert isinstance(msg["request_id"], str) and msg["request_id"]

    def test_show_returns_request_id_matching_event(self, proxy):
        p, queue = proxy
        rid = p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        msg = queue.put.call_args[0][0]
        assert rid == msg["request_id"]

    def test_show_sets_active(self, proxy):
        p, _ = proxy
        p.show("hello", terminal_hwnd=123, geometry=(0, 0, 1920, 1080))
        assert p.is_active

    def test_double_show_ignored(self, proxy):
        p, queue = proxy
        p.show("first", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.show("second", terminal_hwnd=2, geometry=(0, 0, 800, 600))
        # Second show ignored -- only one call.
        assert queue.put.call_count == 1

    def test_show_returns_immediately(self, proxy):
        """show() must not block. It enqueues and returns."""
        p, _ = proxy
        t0 = time.perf_counter()
        rid = p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.05, f"show() should not block; took {elapsed:.3f}s"
        assert rid


class TestProxySubmit:
    def test_submit_sends_event(self, proxy):
        p, queue = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        queue.reset_mock()
        p.submit()
        queue.put.assert_called_once()
        msg = queue.put.call_args[0][0]
        assert msg["type"] == "te_event"
        assert msg["event"] == "submit"

    def test_submit_sets_submit_in_progress(self, proxy):
        p, _ = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.submit()
        assert p._submit_in_progress.is_set()

    def test_submit_when_inactive_noop(self, proxy):
        p, queue = proxy
        p.submit()
        queue.put.assert_not_called()


class TestProxyCancel:
    def test_cancel_sends_event(self, proxy):
        p, queue = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        queue.reset_mock()
        p.cancel()
        queue.put.assert_called_once()
        msg = queue.put.call_args[0][0]
        assert msg["type"] == "te_event"
        assert msg["event"] == "cancel"

    def test_cancel_clears_active(self, proxy):
        p, _ = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.cancel()
        assert not p.is_active


class TestProxySubmitTimeout:
    def test_submit_starts_safety_timer(self, proxy):
        p, _ = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.submit()
        assert p._submit_timer is not None
        assert p._submit_timer.is_alive()
        p._cancel_submit_timer()  # cleanup

    def test_timeout_clears_submit_in_progress(self, proxy):
        p, _ = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.submit()
        p._submit_timeout()
        assert not p._submit_in_progress.is_set()
        assert not p.is_active


class TestProxyForceCleanup:
    def test_force_cleanup_resets_state(self, proxy):
        p, _ = proxy
        p.show("text", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.submit()
        p.force_cleanup()
        assert not p.is_active
        assert not p._submit_in_progress.is_set()
        assert p._submit_timer is None


class TestProxyShowAck:
    """The show ack records the editor HWND for the focus-redirect bridge
    and the retract focus check."""

    def test_show_ack_records_editor_hwnd(self, proxy):
        p, _ = proxy
        rid = p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.on_event_ack(rid, "show", editor_hwnd=98765)
        assert p.editor_hwnd == 98765

    def test_unknown_show_ack_without_hwnd_is_no_op(self, proxy):
        p, _ = proxy
        p.on_event_ack("nonexistent-rid", "show", editor_hwnd=None)
        assert p.editor_hwnd is None


class TestProxyResetSessionState:
    """Editor-close paths must clear the editor HWND."""

    def _seed_session(self, p):
        p.show("hi", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p._editor_hwnd = 98765

    def test_cancel_resets_session_state(self, proxy):
        p, _ = proxy
        self._seed_session(p)
        p.cancel()
        assert p.editor_hwnd is None

    def test_force_cleanup_resets_session_state(self, proxy):
        p, _ = proxy
        self._seed_session(p)
        p.force_cleanup()
        assert p.editor_hwnd is None

    def test_submit_timeout_resets_session_state(self, proxy):
        p, _ = proxy
        self._seed_session(p)
        p.submit()
        p._submit_timeout()
        assert p.editor_hwnd is None


class TestProxySubmitLifecycleAcks:
    """wh-eolas.1.3: submit_complete / submit_failed acks clear proxy state.

    The GUI direct-submit path emits ``submit_started``,
    ``submit_complete``, and ``submit_failed:<reason>`` lifecycle acks
    when the editor handles Enter in-process. The proxy treats
    ``submit_complete`` and ``submit_failed:*`` as editor-close events.
    """

    def test_submit_complete_ack_clears_active_state(self, proxy):
        p, _ = proxy
        p.show("ls", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        assert p.is_active is True

        p.on_event_ack("rid-submit", "submit_complete", editor_hwnd=12345)

        assert p.is_active is False
        assert p._submit_in_progress.is_set() is False
        assert p.editor_hwnd is None

    def test_submit_failed_ack_clears_active_state(self, proxy):
        p, _ = proxy
        p.show("ls", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        assert p.is_active is True

        p.on_event_ack(
            "rid-submit", "submit_failed:foreground_failed", editor_hwnd=0,
        )

        assert p.is_active is False
        assert p._submit_in_progress.is_set() is False
        assert p.editor_hwnd is None

    def test_voice_enter_submit_complete_ack_clears_state(self, proxy):
        """Voice "enter" enters the legacy submit() path, which sets
        ``_submit_in_progress`` and starts the safety timer. When the
        GUI direct-submit path completes and acks ``submit_complete``,
        both pieces of legacy state must clear so the next utterance
        does not strand on the safety timer or skip clipboard restore.
        """
        p, _ = proxy
        p.show("ls", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        # Voice "enter" path.
        p.submit()
        assert p._submit_in_progress.is_set() is True
        assert p._submit_timer is not None

        # GUI direct-submit completes.
        p.on_event_ack("rid-submit", "submit_complete", editor_hwnd=12345)

        assert p.is_active is False
        assert p._submit_in_progress.is_set() is False
        assert p._submit_timer is None

    def test_submit_failed_after_voice_enter_clears_state(self, proxy):
        """Same as above but for the failure ack."""
        p, _ = proxy
        p.show("ls", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        p.submit()
        p.on_event_ack(
            "rid-submit", "submit_failed:clipboard_verify_failed",
            editor_hwnd=0,
        )
        assert p.is_active is False
        assert p._submit_in_progress.is_set() is False
        assert p._submit_timer is None


class TestProxySendFailureRollsBackState:
    """Queue put failures must NOT leave the proxy claiming the GUI
    received the event (wh-oe7u.6).
    """

    @pytest.fixture
    def failing_queue_proxy(self):
        from ui.terminal_editor_proxy import TerminalEditorProxy
        response_queue = MagicMock()
        clipboard_ops = MagicMock(accumulated_paste_chars=0)
        p = TerminalEditorProxy(response_queue=response_queue, clipboard_ops=clipboard_ops)
        return p, response_queue, clipboard_ops

    def test_show_send_failure_returns_none(self, failing_queue_proxy):
        p, queue, _ = failing_queue_proxy
        queue.put.side_effect = RuntimeError("queue closed")
        rid = p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        assert rid is None, (
            "show() must return None on _send_event failure so the "
            "caller can propagate the failure."
        )

    def test_show_send_failure_leaves_proxy_inactive(self, failing_queue_proxy):
        """If the queue rejected the show event the GUI never sees it,
        so claiming is_active=True would block subsequent legitimate
        show() calls."""
        p, queue, _ = failing_queue_proxy
        queue.put.side_effect = RuntimeError("queue closed")
        p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        assert p.is_active is False

    def test_show_send_failure_does_not_set_editor_hwnd(self, failing_queue_proxy):
        """Editor HWND is recorded on ack; it must remain None when the
        send itself failed (no GUI -> no ack -> no HWND)."""
        p, queue, _ = failing_queue_proxy
        queue.put.side_effect = RuntimeError("queue closed")
        p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        assert p.editor_hwnd is None

    def test_show_success_returns_request_id(self, failing_queue_proxy):
        """Sanity: success path is unchanged."""
        p, _, _ = failing_queue_proxy
        rid = p.show("hello", terminal_hwnd=1, geometry=(0, 0, 800, 600))
        assert rid is not None and rid
        assert p.is_active is True
