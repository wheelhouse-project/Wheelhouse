"""insert_editor_word IPC contract (wh-g2-refactor.14).

Defines the Logic -> GUI per-word insert request that fires once per
``WordEvent`` reaching the persistent dictation editor. Each request
becomes a separate round trip on ``state_to_gui_queue`` outbound and
``commands_to_logic_queue`` inbound. Section 5 of
``docs/design/2026-05-20-g2-refactor-design-refinements.md`` is the
authoritative reference.

The producer awaits the response so per-word backpressure matches
today's ``app.send_request("intelligent_insert_text", ...)`` contract.
The request carries ``editor_generation`` for the rebuild fence
(round 2 / codex 7.5); the GUI dispatcher rejects mismatches without
touching the editor.

Round 2 / codex 7.4: ``session_mismatch`` is now a hard reject on the
GUI side. The pre-round-2 behaviour (silently reset the ledger to the
new utterance and insert anyway) is gone, so a late stale request
cannot corrupt the current session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ACTION_NAME_REQUEST = "insert_editor_word"
ACTION_NAME_RESPONSE = "insert_editor_word_response"


FAILURE_SUCCESS = ""
FAILURE_NO_ACTIVE_SESSION = "no_active_session"
FAILURE_SESSION_MISMATCH = "session_mismatch"
# editor_unavailable covers two cases that share the same producer
# response (drop the word, no retry):
#   1. The persistent editor object does not exist (initialisation race
#      before GUI startup completes; should be impossible in production
#      after the GUI is up).
#   2. The editor.insert_word handler raised an unhandled exception
#      (e.g. QTextCursor.insertText assertion). The exception text is
#      logged at WARNING level by EditorIpcResponder before the response
#      is sent; downstream metrics that key on failure_reason should
#      treat editor_unavailable as "editor cannot fulfil this request"
#      rather than strictly "editor is None".
FAILURE_EDITOR_UNAVAILABLE = "editor_unavailable"
FAILURE_STALE_GENERATION = "stale_generation"
FAILURE_EDITOR_REBUILT = "editor_rebuilt"


ALLOWED_FAILURE_REASONS: frozenset[str] = frozenset({
    FAILURE_SUCCESS,
    FAILURE_NO_ACTIVE_SESSION,
    FAILURE_SESSION_MISMATCH,
    FAILURE_EDITOR_UNAVAILABLE,
    FAILURE_STALE_GENERATION,
    FAILURE_EDITOR_REBUILT,
})


class InsertEditorWordSchemaError(ValueError):
    """Raised on a malformed insert_editor_word request or response.

    Consumers should catch this and degrade gracefully (log + drop)
    per wh-uf54 (IPC schema validation and graceful degradation).
    """


def _check_str(name: str, value: Any, *, allow_empty: bool) -> None:
    if not isinstance(value, str):
        raise InsertEditorWordSchemaError(
            f"{name} must be a str, got {type(value).__name__}"
        )
    if not allow_empty and value == "":
        raise InsertEditorWordSchemaError(f"{name} must not be empty")


def _check_int(name: str, value: Any, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InsertEditorWordSchemaError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    if value < minimum:
        raise InsertEditorWordSchemaError(
            f"{name} must be >= {minimum}, got {value}"
        )


@dataclass(frozen=True)
class InsertEditorWordRequest:
    """Logic -> GUI per-word insert request.

    Fields:
      * ``request_id`` -- uuid4 hex; correlates with the response.
      * ``text`` -- non-empty string; the word or sub-word fragment
        to insert. Empty strings are rejected at the boundary because
        the caller already filters them; routing an empty insert
        through Qt would be a no-op that still costs a round trip.
      * ``utterance_id`` -- non-empty string; the originating
        utterance's id.
      * ``editor_generation`` -- non-negative int; the editor
        generation Logic believes is current (round 2 / codex 7.5).
    """

    request_id: str
    text: str
    utterance_id: str
    editor_generation: int

    def __post_init__(self) -> None:
        _check_str("request_id", self.request_id, allow_empty=False)
        _check_str("text", self.text, allow_empty=False)
        _check_str("utterance_id", self.utterance_id, allow_empty=False)
        _check_int("editor_generation", self.editor_generation, minimum=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": ACTION_NAME_REQUEST,
            "request_id": self.request_id,
            "text": self.text,
            "utterance_id": self.utterance_id,
            "editor_generation": self.editor_generation,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "InsertEditorWordRequest":
        if not isinstance(payload, Mapping):
            raise InsertEditorWordSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        action = payload.get("action")
        if action != ACTION_NAME_REQUEST:
            raise InsertEditorWordSchemaError(
                f"action {action!r} does not match {ACTION_NAME_REQUEST!r}"
            )
        for required in ("request_id", "text", "utterance_id", "editor_generation"):
            if required not in payload:
                raise InsertEditorWordSchemaError(
                    f"payload missing required field {required!r}"
                )
        return cls(
            request_id=payload["request_id"],
            text=payload["text"],
            utterance_id=payload["utterance_id"],
            editor_generation=payload["editor_generation"],
        )


@dataclass(frozen=True)
class InsertEditorWordResponse:
    """GUI -> Logic per-word insert response.

    Fields:
      * ``request_id`` -- echoes the request id.
      * ``chars_inserted`` -- non-negative int. UTF-16 code-unit count
        the GUI actually wrote into the document (round 2 / codex
        7.3). For a successful insert this equals ``_utf16_len(text)``,
        which is ``len(text)`` for the BMP characters that dominate
        STT output. This is a Qt cursor-position delta; it is NOT the
        retract accounting unit.
      * ``clusters_inserted`` -- non-negative int. Grapheme-cluster
        count of the inserted run (wh-editor-retract-dup.1.1). The
        retract path peels grapheme clusters and its success invariant
        (``chars_removed == chars_requested``) is in clusters, so the
        speech-side per-utterance editor total accumulates THIS, not
        ``chars_inserted``. For BMP text the two coincide; for
        astral-plane input UTF-16 over-counts and a chars_inserted-based
        retract would over-request and underrun. Defaults to 0 so a
        producer that predates the field (or an error-path response)
        deserialises cleanly; real successful inserts always set it.
      * ``failure_reason`` -- one of the enumerated reasons, ``""``
        for success.
    """

    request_id: str
    chars_inserted: int
    failure_reason: str
    clusters_inserted: int = 0

    def __post_init__(self) -> None:
        _check_str("request_id", self.request_id, allow_empty=False)
        _check_int("chars_inserted", self.chars_inserted, minimum=0)
        _check_int("clusters_inserted", self.clusters_inserted, minimum=0)
        _check_str("failure_reason", self.failure_reason, allow_empty=True)
        if self.failure_reason not in ALLOWED_FAILURE_REASONS:
            raise InsertEditorWordSchemaError(
                f"failure_reason {self.failure_reason!r} is not allowed; "
                f"must be one of {sorted(ALLOWED_FAILURE_REASONS)!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": ACTION_NAME_RESPONSE,
            "request_id": self.request_id,
            "chars_inserted": self.chars_inserted,
            "clusters_inserted": self.clusters_inserted,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "InsertEditorWordResponse":
        if not isinstance(payload, Mapping):
            raise InsertEditorWordSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        action = payload.get("action")
        if action != ACTION_NAME_RESPONSE:
            raise InsertEditorWordSchemaError(
                f"action {action!r} does not match {ACTION_NAME_RESPONSE!r}"
            )
        for required in ("request_id", "chars_inserted", "failure_reason"):
            if required not in payload:
                raise InsertEditorWordSchemaError(
                    f"payload missing required field {required!r}"
                )
        # clusters_inserted is optional on the wire (defaults to 0) so a
        # response produced before the field existed still deserialises.
        return cls(
            request_id=payload["request_id"],
            chars_inserted=payload["chars_inserted"],
            failure_reason=payload["failure_reason"],
            clusters_inserted=payload.get("clusters_inserted", 0),
        )
