"""Proxy for the terminal dictation editor window in the GUI Process.

Runs in the Input Process. Tracks editor open/close lifecycle and forwards
show/cancel/submit commands to the GUI Process via the response queue.

wh-1g6er: this is the slim proxy that survived the terminal-editor
strategy deletion. The focus-redirect path (the only live opener)
calls :meth:`show` with empty text;
drained words flow through StandardStrategy / VerifiedUnicodeStrategy
directly against the editor's QPlainTextEdit and never go through this
proxy. The stale-event tracking, ack-driven retract-counter advancement,
and editor-mirror plumbing that the legacy strategy needed have all been
removed. ``editor_event_acked`` from the GUI editor still flows through
``on_event_ack`` so the focus-redirect bridge (wh-pkhrp) can drive the
LogicMirror through SUBMITTING / SUBMIT_COMPLETE / ERROR.
"""
import logging
import threading
import uuid
from multiprocessing import Queue
from typing import Optional

log = logging.getLogger(__name__)


class TerminalEditorProxy:
    """IPC proxy for the terminal dictation editor in the GUI Process.

    Public surface (live callers only):
      - is_active (property)
      - editor_hwnd (property)
      - show(): open an editor session.
      - cancel(): cancel and hide the editor.
      - submit(): tell the editor to submit; starts a safety timer that
        clears ``_submit_in_progress`` if the GUI never closes the
        session out via an ack.
      - force_cleanup(): reset all proxy state (recovery).
      - on_event_ack(): consumes the editor's lifecycle acks
        (submit_complete / submit_failed / show / focus_*). The
        submit-lifecycle branch is the editor-close path used by the
        wh-eolas GUI-direct submit. The other ops flow through to the
        focus-redirect bridge via the bus; the proxy itself does not
        consume them any more.
      - _submit_in_progress (threading.Event): consulted by
        ``end_utterance`` to skip clipboard restore while a submit is
        in flight.
    """

    # Safety timeout: if a submit lifecycle ack never arrives (IPC
    # failure, GUI crash, etc.), auto-clear _submit_in_progress so
    # end_utterance does not permanently skip clipboard restoration.
    _SUBMIT_TIMEOUT_S = 5.0

    def __init__(self, response_queue: Queue, clipboard_ops=None):
        # ``clipboard_ops`` is retained in the signature so existing
        # callers (UIActionHandler.__init__) keep compiling, but the
        # slim proxy no longer mutates clipboard state -- the
        # strategy-side counter advancement is gone with the strategy
        # (wh-1g6er).
        self._response_queue = response_queue
        self._clipboard_ops = clipboard_ops
        self._is_active = threading.Event()
        self._submit_in_progress = threading.Event()
        self._submit_timer: threading.Timer | None = None
        self._editor_hwnd: Optional[int] = None
        # Serialize state mutation against the _submit_timeout
        # timer-thread callback. The input process is not asyncio
        # based, so a threading.Lock guards the small scalar block.
        self._state_lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        return self._is_active.is_set()

    @property
    def editor_hwnd(self) -> Optional[int]:
        """HWND of the editor window, or None if no show ack has arrived."""
        return self._editor_hwnd

    def start(self, timeout: float = 5.0):
        """No-op -- the GUI Process creates the Qt window."""
        pass

    def stop(self):
        """No-op -- the GUI Process manages the Qt window lifecycle."""
        pass

    def show(
        self,
        initial_text: str,
        terminal_hwnd: int,
        geometry: tuple,
    ) -> Optional[str]:
        """Signal the GUI Process to show the editor.

        Returns a fresh ``request_id`` on a successful enqueue, or
        ``None`` if ``_send_event`` failed. The active flag is set only
        after a successful send so a closed/dead queue does not leave
        the proxy claiming the GUI received an event it never did.

        The focus-redirect path opens an empty editor.
        """
        if self._is_active.is_set():
            log.warning("show() called but editor already active, ignoring.")
            return ""

        request_id = self._new_request_id()
        sent = self._send_event(
            "show",
            request_id=request_id,
            text=initial_text,
            hwnd=terminal_hwnd,
            rect=geometry,
        )
        if not sent:
            return None

        self._is_active.set()
        log.debug(
            "Sent te_event:show (rid=%s, hwnd=%d)",
            request_id, terminal_hwnd,
        )
        return request_id

    def submit(self):
        """Signal the GUI Process to submit editor contents.

        Starts a safety timer that auto-clears ``_submit_in_progress``
        and ``_is_active`` if no submit lifecycle ack arrives. Without
        this, a stuck flag permanently breaks clipboard restore on
        ``end_utterance``.
        """
        if not self._is_active.is_set():
            return

        self._submit_in_progress.set()
        self._cancel_submit_timer()
        self._submit_timer = threading.Timer(
            self._SUBMIT_TIMEOUT_S, self._submit_timeout,
        )
        self._submit_timer.daemon = True
        self._submit_timer.start()

        self._send_event("submit")
        log.debug("Sent te_event:submit")

    def cancel(self):
        """Signal the GUI Process to cancel and hide the editor."""
        if not self._is_active.is_set():
            return

        self._is_active.clear()
        self._reset_session_state()
        self._send_event("cancel")
        log.debug("Sent te_event:cancel")

    # -- State callbacks (called by input_proc command handlers) --

    def on_event_ack(
        self,
        request_id: str,
        op: str,
        editor_hwnd: Optional[int] = None,
    ):
        """Consume editor lifecycle acks.

        The wh-eolas GUI direct-submit path emits ``submit_complete``
        and ``submit_failed:<reason>`` acks on every submit attempt.
        Treat them as editor-close events: clear ``_is_active``,
        cancel the safety timer, clear ``_submit_in_progress``, and
        reset per-session state.

        ``show`` acks record the editor HWND so callers that need it
        (focus-redirect bridge, retract focus check) have a real value
        to compare against.

        Other ops (``focus_confirmed``, ``focus_lost``, ``append``) are
        not consumed by the proxy; the focus-redirect bridge handles
        them via the same fan-out in
        ``main._handle_te_event_ack``.
        """
        if op == "submit_complete" or op.startswith("submit_failed"):
            log.debug(
                "on_event_ack: submit-lifecycle ack op=%s rid=%s; "
                "clearing proxy state",
                op, request_id,
            )
            self._cancel_submit_timer()
            self._is_active.clear()
            self._submit_in_progress.clear()
            self._reset_session_state()
            return

        if op == "show" and editor_hwnd is not None:
            self._editor_hwnd = editor_hwnd
            log.debug(
                "show ack: recorded editor_hwnd=%s (rid=%s)",
                editor_hwnd, request_id,
            )
            return

        log.debug(
            "on_event_ack: ignoring op=%s rid=%s (not a proxy-consumed event)",
            op, request_id,
        )

    # -- Recovery --

    def force_cleanup(self):
        """Reset all proxy state (recovery from stuck state)."""
        log.warning("Force-cleaning terminal editor proxy state.")
        self._cancel_submit_timer()
        self._is_active.clear()
        self._submit_in_progress.clear()
        self._reset_session_state()

    def _submit_timeout(self):
        """Safety timeout: clear submit flags if no lifecycle ack arrived."""
        log.warning(
            "Submit safety timeout (%.0fs): clearing _submit_in_progress. "
            "The GUI lifecycle ack may have been lost.",
            self._SUBMIT_TIMEOUT_S,
        )
        self._submit_in_progress.clear()
        self._is_active.clear()
        self._reset_session_state()

    def _cancel_submit_timer(self):
        if self._submit_timer is not None:
            self._submit_timer.cancel()
            self._submit_timer = None

    # -- Internal --

    def _new_request_id(self) -> str:
        return uuid.uuid4().hex

    def _reset_session_state(self) -> None:
        """Clear all per-session state in one place.

        Called from every editor-close path. Holds ``_state_lock``
        across the mutation so the timer-thread callback cannot
        observe a partially-cleared state while the input-process
        main loop is also resetting.
        """
        with self._state_lock:
            self._editor_hwnd = None

    def _send_event(self, event: str, **kwargs) -> bool:
        """Put an unsolicited event on the response queue.

        Returns True on a successful put, False if the queue rejected
        the message. Callers (show) must mutate proxy state ONLY
        after a True return so a dead/closed queue does not leave the
        proxy claiming the GUI received the event.
        """
        msg = {"type": "te_event", "event": event, **kwargs}
        try:
            self._response_queue.put(msg)
            return True
        except Exception as e:
            log.error("Failed to send te_event:%s: %s", event, e)
            return False
