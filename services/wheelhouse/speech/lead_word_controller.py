"""Logic-process side of the 'Your training word' window (wh-first-sound-lost.23).

The window (``lead_word_dialog.py``) runs in the GUI process and holds no
session logic. This controller runs in the Logic process. It makes the
request IDs, sends the lead_word_* commands to the active Parakeet provider,
reads the provider's ``lead_word_result`` messages, and sends the window one
``lw_state`` message for each change. The message contract is in the bead's
acceptance field (item 10) and its contract note.

The controller is synchronous and does no I/O of its own. Every side effect is an injected callable:

- ``send_to_gui(message)`` puts a ``{"action": "lw_state", ...}`` dict on the
  Logic-to-GUI state queue.
- ``provider`` has three methods: ``send_command(command, request_id)``
  (True when the command left this process), ``hold_transcription(done)``
  (turn transcription off, then call ``done``), and
  ``release_transcription()`` (put the engine's status back).
- ``schedule(delay_s, callback)`` arms a one-shot timer and returns a handle
  with ``.cancel()`` (loop.call_later in production).

The window shows the result of the request that the controller sent last.
A result with another request ID is ignored (acceptance item 10).
"""

import asyncio
import logging
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Screens the controller reports in ``lw_state``.
SCREEN_CLOSED = "closed"
SCREEN_LOADING = "loading"
SCREEN_IDLE = "idle"
SCREEN_LISTENING = "listening"
SCREEN_CHECKING = "checking"
SCREEN_PASSED = "passed"
SCREEN_REFUSED = "refused"

# The seven commands, keyed by the GUI action that asks for each one.
_COMMAND_FOR_ACTION = {
    "lw_record": "lead_word_record",
    "lw_keep": "lead_word_keep",
    "lw_discard": "lead_word_discard",
    "lw_restore": "lead_word_restore",
    "lw_off": "lead_word_off",
    "lw_on": "lead_word_on",
}

# Playback: transcription stays off for the playback length plus this long
# (acceptance item 22).
PLAYBACK_TAIL_S = 0.3
# The longest playback the controller accepts. A recording is at most
# 4.5 s (capture limit plus margins); a larger value is a malformed message.
PLAYBACK_MAX_MS = 60_000
# A request that gets no answer from the provider ends with an error after
# this long, so the window can never wait for ever. Each progress result
# (listening, checking) arms the timer again.
RESPONSE_TIMEOUT_S = 45.0

_FIELDS_OF_STATE_RESULTS = (
    "loaded", "recorded", "microphone", "previous_exists", "current_exists",
)


def _asyncio_schedule(delay_s: float, callback: Callable[[], None]):
    """Default scheduler: loop.call_later on the running loop.

    Returns None when no loop is running (bare unit-test contexts); the
    controller treats a None handle as an unarmed timer.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.call_later(delay_s, callback)


def _cancel(handle: Any) -> None:
    if handle is not None:
        try:
            handle.cancel()
        except Exception:
            logger.debug("Lead-word timer cancel failed", exc_info=True)


class LeadWordController:
    """Owns the state of one 'Your training word' window."""

    def __init__(
        self,
        send_to_gui: Callable[[dict], None],
        provider: Any,
        schedule: Optional[Callable[..., Any]] = None,
        new_request_id: Optional[Callable[[], str]] = None,
    ):
        self._send_to_gui = send_to_gui
        self._provider = provider
        self._schedule = schedule if schedule is not None else _asyncio_schedule
        self._new_request_id = (
            new_request_id if new_request_id is not None
            else (lambda: uuid.uuid4().hex)
        )

        # Whether the active provider declared lead-word support in its
        # capabilities frame. Absent field = False.
        self._supported = False
        self._open = False
        self._current_request_id: Optional[str] = None
        self._guard_handle: Any = None
        self._play_handle: Any = None
        self._playing = False
        self._reset_view()

    def _reset_view(self) -> None:
        self._screen = SCREEN_CLOSED
        self._busy = False
        # What the provider last said about the files and the engine.
        self._info = {
            "loaded": False,
            "recorded": "",
            "microphone": "",
            "previous_exists": False,
            "current_exists": False,
        }
        self._key_path = ""
        self._candidate = False
        self._measures: dict = {}
        # True while the candidate shown came with the provider's noise
        # advice (wh-training-word-noisy-reject). A provider older than the
        # field does not send it, and the window then shows no advice.
        self._noise_advice = False
        self._reason = ""
        self._error = ""
        self._notice = ""

    # ------------------------------------------------------------------ #
    #  Public surface
    # ------------------------------------------------------------------ #

    @property
    def current_request_id(self) -> Optional[str]:
        return self._current_request_id

    @property
    def screen(self) -> str:
        return self._screen

    @property
    def window_open(self) -> bool:
        return self._open

    def set_supported(self, supported: bool) -> None:
        """Record the capabilities frame of the active provider.

        Called by the WebSocket manager on a capabilities frame, and with
        False when the active provider goes away. A provider that supports
        the word and (re)connects while the window is open starts a new
        status request, because a restarted provider forgot any recording
        in progress.
        """
        supported = bool(supported)
        self._supported = supported
        if not self._open:
            return
        if not supported:
            self._cancel_request_timer()
            self._current_request_id = None
            self._release_playback()
            self._screen = SCREEN_LOADING
            self._busy = False
            self._push()
            return
        self._request_status()

    def handle_gui_action(self, action: str, command: Optional[dict] = None) -> None:
        """Handle one lw_* action from the GUI process."""
        command = command if isinstance(command, dict) else {}
        try:
            if action == "lw_open":
                self._on_open()
            elif action == "lw_close":
                self._on_close()
            elif action == "lw_play_start":
                self._on_play_start(command)
            elif action == "lw_play_end":
                self._release_playback()
                self._push()
            elif action in _COMMAND_FOR_ACTION:
                self._on_command(action)
            else:
                logger.warning("Unknown lead-word action %r ignored", action)
        except Exception:
            logger.exception("Lead-word action %r failed", action)

    def on_result(self, data: dict) -> None:
        """Handle one ``lead_word_result`` message from the active provider."""
        try:
            self._on_result(data)
        except Exception:
            logger.exception("Lead-word result handling failed")

    def on_send_failed(self, request_id: str) -> None:
        """Handle a command whose asynchronous send did not reach the provider.

        ``send_command`` returns True once the send is scheduled, so a send
        that fails later (no active client, a closed socket) reports here.
        A failure of a request that is no longer the current one is ignored.
        """
        if not self._open or request_id != self._current_request_id:
            return
        self._on_send_failed()
        self._push()

    # ------------------------------------------------------------------ #
    #  GUI actions
    # ------------------------------------------------------------------ #

    def _on_open(self) -> None:
        self._open = True
        self._reset_view()
        self._release_playback()
        if not self._supported:
            self._screen = SCREEN_LOADING
            self._push()
            return
        self._request_status()

    def _on_close(self) -> None:
        if not self._open:
            return
        # Item 26: a recording that is running, or a candidate that waits
        # for Keep, ends with the window. The provider leaves recording
        # mode and deletes the candidate.
        needs_discard = self._screen in (SCREEN_LISTENING, SCREEN_CHECKING) or (
            self._candidate
        )
        self._release_playback()
        self._cancel_request_timer()
        if needs_discard and self._supported:
            self._send("lead_word_discard")
        self._open = False
        self._current_request_id = None
        self._reset_view()

    def _on_command(self, action: str) -> None:
        if not self._open or not self._supported:
            return
        if self._busy or self._playing:
            logger.info("Lead-word action %s ignored: a request is running", action)
            return
        in_recording = self._screen in (SCREEN_LISTENING, SCREEN_CHECKING)
        if action == "lw_record":
            if in_recording:
                # One press makes one take.
                return
            self._candidate = False
            self._measures = {}
            self._noise_advice = False
            self._reason = ""
            self._error = ""
            self._notice = ""
            self._screen = SCREEN_LISTENING
            self._send("lead_word_record")
            self._arm_request_timer()
            self._push()
            return
        if in_recording and action != "lw_discard":
            return
        if action == "lw_keep" and not self._candidate:
            return
        if action == "lw_discard":
            self._candidate = False
            self._measures = {}
            self._noise_advice = False
            self._reason = ""
            self._error = ""
            self._notice = ""
            self._screen = SCREEN_IDLE
        else:
            self._error = ""
            self._notice = ""
        self._busy = True
        self._send(_COMMAND_FOR_ACTION[action])
        self._arm_request_timer()
        self._push()

    def _on_play_start(self, command: dict) -> None:
        if not self._open or self._playing or self._busy:
            return
        if self._screen in (SCREEN_LISTENING, SCREEN_CHECKING, SCREEN_LOADING):
            return
        duration_ms = command.get("duration_ms")
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms <= 0
            or duration_ms > PLAYBACK_MAX_MS
        ):
            logger.warning("Lead-word playback refused: bad duration %r", duration_ms)
            return
        which = command.get("which")
        if which not in ("current", "candidate"):
            logger.warning("Lead-word playback refused: bad file %r", which)
            return
        self._playing = True
        # If the engine is never told, the timer still ends the hold.
        _cancel(self._play_handle)
        self._play_handle = self._schedule(RESPONSE_TIMEOUT_S, self._playback_ended)
        # Transcription is off before the GUI process plays the sound. The
        # provider channel calls ``done`` once the engine has been told.
        self._provider.hold_transcription(
            lambda: self._after_hold(which, duration_ms)
        )
        self._push()

    def _after_hold(self, which: str, duration_ms: int) -> None:
        if not self._playing:
            # The window closed while the engine was being told. The hold
            # is already released; do not start a sound nobody can stop.
            return
        self._send_to_gui({"action": "lw_play_go", "which": which})
        _cancel(self._play_handle)
        self._play_handle = self._schedule(
            duration_ms / 1000.0 + PLAYBACK_TAIL_S, self._playback_ended
        )

    def _playback_ended(self) -> None:
        self._play_handle = None
        self._release_playback()
        self._push()

    def _release_playback(self) -> None:
        _cancel(self._play_handle)
        self._play_handle = None
        if self._playing:
            self._playing = False
            try:
                self._provider.release_transcription()
            except Exception:
                logger.exception("Lead-word transcription release failed")

    # ------------------------------------------------------------------ #
    #  Provider results
    # ------------------------------------------------------------------ #

    def _on_result(self, data: dict) -> None:
        if not isinstance(data, dict) or not self._open:
            return
        if data.get("request_id") != self._current_request_id:
            logger.debug(
                "Lead-word result for another request ignored (stage=%r)",
                data.get("stage"),
            )
            return
        stage = data.get("stage")
        if stage in ("listening", "checking"):
            self._screen = (
                SCREEN_LISTENING if stage == "listening" else SCREEN_CHECKING
            )
            self._arm_request_timer()
            self._push()
            return
        # Every other stage ends the request.
        self._cancel_request_timer()
        self._busy = False
        if stage == "passed":
            self._candidate = True
            self._measures = _dict_or_empty(data.get("measures"))
            self._noise_advice = data.get("noise_advice") is True
            self._reason = ""
            self._error = ""
            self._notice = ""
            self._key_path = _text(data.get("key_path"), self._key_path)
            self._screen = SCREEN_PASSED
        elif stage == "refused":
            self._candidate = False
            self._measures = _dict_or_empty(data.get("measures"))
            self._noise_advice = False
            self._reason = _text(data.get("reason"))
            self._error = ""
            self._notice = ""
            self._screen = SCREEN_REFUSED
        elif stage == "failed":
            self._error = _text(data.get("reason"), "The request failed.")
            self._notice = ""
            self._screen = SCREEN_PASSED if self._candidate else SCREEN_IDLE
        elif stage in ("status", "kept", "restored", "off", "on"):
            self._take_info(data)
            if stage == "status":
                self._key_path = _text(data.get("key_path"), self._key_path)
            self._candidate = False
            self._measures = {}
            self._noise_advice = False
            self._reason = ""
            self._error = ""
            self._notice = "" if stage == "status" else stage
            self._screen = SCREEN_IDLE
        else:
            logger.warning("Lead-word result with unknown stage %r ignored", stage)
            return
        self._push()

    def _take_info(self, data: dict) -> None:
        for key in _FIELDS_OF_STATE_RESULTS:
            if key not in data:
                continue
            if key in ("loaded", "previous_exists", "current_exists"):
                self._info[key] = data[key] is True
            else:
                self._info[key] = _text(data[key])
        # A provider older than current_exists (wh-first-sound-lost.23.1.1)
        # does not send it; its recorded date then decides, as before. This
        # is the only place that fallback lives: the window reads
        # current_exists alone.
        if "current_exists" not in data:
            self._info["current_exists"] = self._info["recorded"] != ""

    # ------------------------------------------------------------------ #
    #  Requests and state
    # ------------------------------------------------------------------ #

    def _request_status(self) -> None:
        self._error = ""
        self._notice = ""
        self._screen = SCREEN_LOADING
        self._busy = True
        self._candidate = False
        self._send("lead_word_status")
        self._arm_request_timer()
        self._push()

    def _send(self, command: str) -> None:
        """Send one command with a new request ID; it becomes the current one."""
        request_id = self._new_request_id()
        self._current_request_id = request_id
        try:
            sent = self._provider.send_command(command, request_id)
        except Exception:
            logger.exception("Lead-word command %s could not be sent", command)
            sent = False
        if not sent:
            self._on_send_failed()

    def _on_send_failed(self) -> None:
        # The command never left this process: no answer can come.
        self._cancel_request_timer()
        self._current_request_id = None
        self._busy = False
        self._error = "WheelHouse could not reach the speech engine."
        self._notice = ""
        self._screen = SCREEN_PASSED if self._candidate else SCREEN_IDLE

    def _arm_request_timer(self) -> None:
        self._cancel_request_timer()
        request_id = self._current_request_id
        if request_id is None:
            return
        self._guard_handle = self._schedule(
            RESPONSE_TIMEOUT_S, lambda: self._request_timed_out(request_id)
        )

    def _cancel_request_timer(self) -> None:
        _cancel(self._guard_handle)
        self._guard_handle = None

    def _request_timed_out(self, request_id: str) -> None:
        self._guard_handle = None
        if request_id != self._current_request_id or not self._open:
            return
        # A recording that got no answer can still be pending or running
        # in the provider; without a discard it would take the next
        # utterance as a hidden take.
        was_recording = self._screen in (SCREEN_LISTENING, SCREEN_CHECKING)
        self._current_request_id = None
        self._busy = False
        self._screen = SCREEN_PASSED if self._candidate else SCREEN_IDLE
        if was_recording and self._supported:
            self._send("lead_word_discard")
        self._error = "The speech engine did not answer."
        self._notice = ""
        self._push()

    def _state_payload(self) -> dict:
        return {
            "supported": self._supported,
            "screen": self._screen,
            "busy": self._busy,
            "playing": self._playing,
            "loaded": self._info["loaded"],
            "recorded": self._info["recorded"],
            "microphone": self._info["microphone"],
            "previous_exists": self._info["previous_exists"],
            "current_exists": self._info["current_exists"],
            "key_path": self._key_path,
            "candidate": self._candidate,
            "measures": dict(self._measures),
            "noise_advice": self._noise_advice,
            "reason": self._reason,
            "error": self._error,
            "notice": self._notice,
        }

    def _push(self) -> None:
        if not self._open:
            return
        self._send_to_gui({"action": "lw_state", "state": self._state_payload()})


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _dict_or_empty(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}
