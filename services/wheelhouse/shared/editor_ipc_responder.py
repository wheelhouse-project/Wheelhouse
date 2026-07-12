"""GUI-side responder for the editor IPC protocol (wh-g2-refactor.14).

Dispatches ``insert_editor_word`` and ``retract_editor_text`` requests
on the Qt main thread and enqueues correlated responses on
``commands_to_logic_queue``. The responder is a thin glue layer
between the raw queue dict (which the GUI's main dispatcher already
pulls off ``state_to_gui_queue``) and the editor's typed methods
(``insert_word`` / ``retract_and_replay`` -- specified in Sections 3
and 5 of the G2 design refinements).

The responder owns the rebuild-fence check (Section 6). Three
generation-related paths short-circuit before the editor is touched:

  * No live editor (``get_editor() is None``) -> ``stale_generation``.
  * Request's ``editor_generation`` does not match the live editor's
    counter -> ``stale_generation``.

Both paths enqueue a well-formed response so the Logic-side future
resolves promptly. The Section 5 / Section 2 pseudocode explicitly
chooses ``stale_generation`` over ``editor_unavailable`` for the
no-live-editor case so the Logic-side drop-without-retry branch fires
cleanly.

Malformed payloads (failures to parse the request schema) are caught
and dropped silently here. Logic will fall through to its
``asyncio.wait_for`` timeout; this matches wh-uf54's graceful
degradation rule for new events and avoids feeding a corrupted
response back through the boundary check.

A handler exception (e.g. the editor's ``insert_word`` raised) is
caught and surfaces as ``editor_unavailable`` so a Qt-side bug does
not crash the GUI's main dispatcher.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from services.wheelhouse.shared.insert_editor_word import (
    ACTION_NAME_REQUEST as INSERT_ACTION_REQUEST,
    FAILURE_EDITOR_UNAVAILABLE as INSERT_EDITOR_UNAVAILABLE,
    FAILURE_STALE_GENERATION as INSERT_STALE_GENERATION,
    InsertEditorWordRequest,
    InsertEditorWordResponse,
    InsertEditorWordSchemaError,
)
from services.wheelhouse.shared.retract_editor_text import (
    ACTION_NAME_REQUEST as RETRACT_ACTION_REQUEST,
    FAILURE_EDITOR_UNAVAILABLE as RETRACT_EDITOR_UNAVAILABLE,
    FAILURE_STALE_GENERATION as RETRACT_STALE_GENERATION,
    RetractEditorTextRequest,
    RetractEditorTextResponse,
    RetractEditorTextSchemaError,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InsertHandlerResult:
    """Return shape from ``editor.insert_word(...)``.

    Mirrors Section 5's ``InsertResult`` dataclass declared on the
    terminal editor. Re-declared here so the responder does not
    depend on the editor module (and so the unit tests can stub the
    handler with a plain object).
    """

    chars_inserted: int
    failure_reason: str


@dataclass(frozen=True)
class RetractHandlerResult:
    """Return shape from ``editor.retract_and_replay(...)``.

    Mirrors Section 2's ``RetractResult`` dataclass declared on the
    terminal editor.
    """

    chars_removed: int
    replay_chars: int
    failure_reason: str


class EditorIpcResponder:
    """Dispatches editor IPC requests on the Qt main thread.

    Parameters:
      * ``get_editor`` -- a zero-arg callable returning the live
        ``TerminalDictationEditorWindow`` (or any object with the
        same protocol: ``_editor_generation`` attribute, ``insert_word``
        and ``retract_and_replay`` methods). Returns ``None`` if no
        editor is live (rebuild in flight).
      * ``response_queue`` -- the GUI -> Logic queue
        (``commands_to_logic_queue``). The responder calls
        ``put_nowait`` on it; the queue must be unbounded or sized
        generously enough that the GUI process never blocks here.

    Call ``handle(message)`` from the GUI's main state-queue
    dispatcher. The method returns ``True`` when it consumed the
    message and ``False`` when the action was not one of the editor
    IPC actions (so the host dispatcher can keep matching).

    The class is NOT thread-safe; it must be driven only from the Qt
    main thread (the same thread the host dispatcher runs on).
    """

    _INSERT_ACTION = INSERT_ACTION_REQUEST
    _RETRACT_ACTION = RETRACT_ACTION_REQUEST

    def __init__(
        self,
        get_editor: Callable[[], Optional[Any]],
        response_queue: Any,
    ) -> None:
        self._get_editor = get_editor
        self._response_queue = response_queue

    def handle(self, message: Any) -> bool:
        """Dispatch a queue message; return True iff consumed."""
        if not isinstance(message, dict):
            return False
        action = message.get("action")
        if action == self._INSERT_ACTION:
            self._handle_insert(message)
            return True
        if action == self._RETRACT_ACTION:
            self._handle_retract(message)
            return True
        return False

    # ------------------------------------------------------------------
    # Insert
    # ------------------------------------------------------------------

    def _handle_insert(self, message: dict) -> None:
        try:
            request = InsertEditorWordRequest.from_dict(message)
        except InsertEditorWordSchemaError as exc:
            logger.warning("Dropping malformed insert_editor_word: %s", exc)
            return

        editor = self._get_editor()
        if editor is None:
            # Section 5: prefer stale_generation over editor_unavailable
            # so Logic's drop-without-retry branch fires.
            self._enqueue_insert_response(
                request.request_id, 0, INSERT_STALE_GENERATION,
            )
            return

        try:
            live_generation = editor._editor_generation
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "insert_editor_word: editor._editor_generation read failed: %s",
                exc,
            )
            self._enqueue_insert_response(
                request.request_id, 0, INSERT_EDITOR_UNAVAILABLE,
            )
            return

        if request.editor_generation != live_generation:
            # Section 6 rebuild fence.
            self._enqueue_insert_response(
                request.request_id, 0, INSERT_STALE_GENERATION,
            )
            return

        try:
            result = editor.insert_word(request.text, request.utterance_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "insert_editor_word: editor.insert_word raised: %s", exc,
            )
            self._enqueue_insert_response(
                request.request_id, 0, INSERT_EDITOR_UNAVAILABLE,
            )
            return

        chars_inserted = int(getattr(result, "chars_inserted", 0))
        clusters_inserted = int(getattr(result, "clusters_inserted", 0))
        failure_reason = str(getattr(result, "failure_reason", ""))
        self._enqueue_insert_response(
            request.request_id, chars_inserted, failure_reason,
            clusters_inserted,
        )

    def _enqueue_insert_response(
        self,
        request_id: str,
        chars_inserted: int,
        failure_reason: str,
        clusters_inserted: int = 0,
    ) -> None:
        try:
            response = InsertEditorWordResponse(
                request_id=request_id,
                chars_inserted=chars_inserted,
                failure_reason=failure_reason,
                clusters_inserted=clusters_inserted,
            )
        except InsertEditorWordSchemaError as exc:
            logger.warning(
                "insert_editor_word: malformed handler result "
                "(rid=%s, chars=%d, reason=%r): %s",
                request_id, chars_inserted, failure_reason, exc,
            )
            return
        try:
            self._response_queue.put_nowait(response.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "insert_editor_word: failed to enqueue response (rid=%s): %s",
                request_id, exc,
            )

    # ------------------------------------------------------------------
    # Retract
    # ------------------------------------------------------------------

    def _handle_retract(self, message: dict) -> None:
        try:
            request = RetractEditorTextRequest.from_dict(message)
        except RetractEditorTextSchemaError as exc:
            logger.warning("Dropping malformed retract_editor_text: %s", exc)
            return

        editor = self._get_editor()
        if editor is None:
            self._enqueue_retract_response(
                request.request_id,
                request.chars_requested,
                0,
                0,
                RETRACT_STALE_GENERATION,
                request.whole_utterance,
            )
            return

        try:
            live_generation = editor._editor_generation
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "retract_editor_text: editor._editor_generation read failed: %s",
                exc,
            )
            self._enqueue_retract_response(
                request.request_id,
                request.chars_requested,
                0,
                0,
                RETRACT_EDITOR_UNAVAILABLE,
                request.whole_utterance,
            )
            return

        if request.editor_generation != live_generation:
            self._enqueue_retract_response(
                request.request_id,
                request.chars_requested,
                0,
                0,
                RETRACT_STALE_GENERATION,
                request.whole_utterance,
            )
            return

        try:
            result = editor.retract_and_replay(
                request.chars_requested,
                utterance_id=request.utterance_id,
                replay_text=request.replay_text,
                whole_utterance=request.whole_utterance,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "retract_editor_text: editor.retract_and_replay raised: %s",
                exc,
            )
            self._enqueue_retract_response(
                request.request_id,
                request.chars_requested,
                0,
                0,
                RETRACT_EDITOR_UNAVAILABLE,
                request.whole_utterance,
            )
            return

        chars_removed = int(getattr(result, "chars_removed", 0))
        replay_chars = int(getattr(result, "replay_chars", 0))
        failure_reason = str(getattr(result, "failure_reason", ""))
        self._enqueue_retract_response(
            request.request_id,
            request.chars_requested,
            chars_removed,
            replay_chars,
            failure_reason,
            request.whole_utterance,
        )

    def _enqueue_retract_response(
        self,
        request_id: str,
        chars_requested: int,
        chars_removed: int,
        replay_chars: int,
        failure_reason: str,
        whole_utterance: bool = False,
    ) -> None:
        try:
            response = RetractEditorTextResponse(
                request_id=request_id,
                chars_requested=chars_requested,
                chars_removed=chars_removed,
                replay_chars=replay_chars,
                failure_reason=failure_reason,
                whole_utterance=whole_utterance,
            )
        except RetractEditorTextSchemaError as exc:
            logger.warning(
                "retract_editor_text: malformed handler result "
                "(rid=%s, req=%d, removed=%d, replay=%d, reason=%r): %s",
                request_id,
                chars_requested,
                chars_removed,
                replay_chars,
                failure_reason,
                exc,
            )
            return
        try:
            self._response_queue.put_nowait(response.to_dict())
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "retract_editor_text: failed to enqueue response (rid=%s): %s",
                request_id, exc,
            )
