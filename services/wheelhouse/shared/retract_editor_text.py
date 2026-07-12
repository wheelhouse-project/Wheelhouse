"""retract_editor_text IPC contract (wh-g2-refactor.14).

Defines the Logic -> GUI retract-and-replay request that fires when the
speech processor needs to undo previously-inserted text in the persistent
dictation editor and optionally replay corrected text in the same Qt
main-thread call. Section 2 of
``docs/design/2026-05-20-g2-refactor-design-refinements.md`` is the
authoritative reference.

The request carries the editor generation Logic believes is current; the
GUI dispatcher rejects mismatches with ``failure_reason =
"stale_generation"``. A non-empty ``replay_text`` instructs the GUI to
insert the corrected text immediately after the retract on the same Qt
main-thread call, closing the paste-vs-ack data-loss window deepseek
flagged in ``wh-g2-refactor.4.1``.

Transport: Logic puts a dict produced by ``RetractEditorTextRequest.
to_dict()`` on ``state_to_gui_queue`` and registers a future in its
own ``_retract_pending`` map keyed by ``request_id``. The GUI process
dispatches the request on the Qt main thread, performs the retract and
optional replay, and enqueues a dict produced by
``RetractEditorTextResponse.to_dict()`` on
``commands_to_logic_queue``. Logic correlates the response back to the
pending future via ``request_id``.

Success-path invariant (round 1 / codex finding C, wh-g2-refactor.5.3):
on ``failure_reason == ""`` the schema requires
``chars_removed == chars_requested``. A partial retract rides the
``ledger_underrun`` branch instead, which the speech processor handles
without a replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Action names and failure-reason constants
# ---------------------------------------------------------------------------


ACTION_NAME_REQUEST = "retract_editor_text"
ACTION_NAME_RESPONSE = "retract_editor_text_response"


FAILURE_SUCCESS = ""
FAILURE_LEDGER_UNDERRUN = "ledger_underrun"
FAILURE_NO_ACTIVE_SESSION = "no_active_session"
FAILURE_SESSION_MISMATCH = "session_mismatch"
# editor_unavailable covers two cases that share the same producer
# response (drop the word, no retry):
#   1. The persistent editor object does not exist (initialisation race
#      before GUI startup completes; should be impossible in production
#      after the GUI is up).
#   2. The editor.retract_and_replay handler raised an unhandled
#      exception (e.g. ledger validation error). The exception text is
#      logged at WARNING level by EditorIpcResponder before the response
#      is sent; downstream metrics that key on failure_reason should
#      treat editor_unavailable as "editor cannot fulfil this request"
#      rather than strictly "editor is None".
FAILURE_EDITOR_UNAVAILABLE = "editor_unavailable"
FAILURE_REPLAY_FAILED = "replay_failed"
FAILURE_STALE_GENERATION = "stale_generation"
FAILURE_EDITOR_REBUILT = "editor_rebuilt"


ALLOWED_FAILURE_REASONS: frozenset[str] = frozenset({
    FAILURE_SUCCESS,
    FAILURE_LEDGER_UNDERRUN,
    FAILURE_NO_ACTIVE_SESSION,
    FAILURE_SESSION_MISMATCH,
    FAILURE_EDITOR_UNAVAILABLE,
    FAILURE_REPLAY_FAILED,
    FAILURE_STALE_GENERATION,
    FAILURE_EDITOR_REBUILT,
})


class RetractEditorTextSchemaError(ValueError):
    """Raised on a malformed retract_editor_text request or response.

    Logic and GUI consumers should catch this and degrade gracefully
    (log + drop the message) per wh-uf54 (IPC schema validation and
    graceful degradation for new events).
    """


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


def _check_str(name: str, value: Any, *, allow_empty: bool) -> None:
    if not isinstance(value, str):
        raise RetractEditorTextSchemaError(
            f"{name} must be a str, got {type(value).__name__}"
        )
    if not allow_empty and value == "":
        raise RetractEditorTextSchemaError(f"{name} must not be empty")


def _check_int(name: str, value: Any, *, minimum: int) -> None:
    # bool is a subclass of int in Python; reject explicitly so a
    # callsite that smuggles a True in for an int field is caught.
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetractEditorTextSchemaError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    if value < minimum:
        raise RetractEditorTextSchemaError(
            f"{name} must be >= {minimum}, got {value}"
        )


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetractEditorTextRequest:
    """Logic -> GUI request payload for retract-and-replay.

    Fields:
      * ``request_id`` -- uuid4 hex string used by Logic to correlate
        the inbound response with the awaiting future.
      * ``chars_requested`` -- positive int. Number of grapheme clusters
        (not UTF-16 code units) to remove from the editor's ledger.
      * ``utterance_id`` -- non-empty string. The originating
        utterance's id; the GUI rejects with ``session_mismatch`` if
        its current ledger session does not match.
      * ``replay_text`` -- string (may be empty). When non-empty, the
        GUI inserts the corrected text immediately after the retract on
        the same Qt main-thread call.
      * ``editor_generation`` -- non-negative int. The editor
        generation Logic believes is current (round 2 / codex 7.5).
        Mismatches rebuild-fence to ``stale_generation``.
    """

    request_id: str
    chars_requested: int
    utterance_id: str
    replay_text: str
    editor_generation: int
    # wh-editor-retract-ledger-authoritative: True selects the
    # whole-utterance mode -- the GUI peels ALL ledger runs for the
    # utterance regardless of chars_requested, which becomes advisory
    # (the speech-side mirror value, carried for diagnostics only).
    # MODE3 retraction uses this so an insert response that timed out
    # Logic-side (word landed, mirror never credited) cannot cause an
    # under-delete.
    whole_utterance: bool = False

    def __post_init__(self) -> None:
        _check_str("request_id", self.request_id, allow_empty=False)
        if not isinstance(self.whole_utterance, bool):
            raise RetractEditorTextSchemaError(
                "whole_utterance must be a bool, got "
                f"{type(self.whole_utterance).__name__}"
            )
        # Whole-utterance mode allows 0 (a fully-drifted mirror may
        # legitimately read 0 while the ledger holds content); counted
        # mode keeps the original >= 1 contract.
        minimum = 0 if self.whole_utterance else 1
        _check_int("chars_requested", self.chars_requested, minimum=minimum)
        _check_str("utterance_id", self.utterance_id, allow_empty=False)
        _check_str("replay_text", self.replay_text, allow_empty=True)
        _check_int("editor_generation", self.editor_generation, minimum=0)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict."""
        return {
            "action": ACTION_NAME_REQUEST,
            "request_id": self.request_id,
            "chars_requested": self.chars_requested,
            "utterance_id": self.utterance_id,
            "replay_text": self.replay_text,
            "editor_generation": self.editor_generation,
            "whole_utterance": self.whole_utterance,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "RetractEditorTextRequest":
        """Parse and validate a wire-format request dict."""
        if not isinstance(payload, Mapping):
            raise RetractEditorTextSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        action = payload.get("action")
        if action != ACTION_NAME_REQUEST:
            raise RetractEditorTextSchemaError(
                f"action {action!r} does not match {ACTION_NAME_REQUEST!r}"
            )
        for required in (
            "request_id",
            "chars_requested",
            "utterance_id",
            "replay_text",
            "editor_generation",
        ):
            if required not in payload:
                raise RetractEditorTextSchemaError(
                    f"payload missing required field {required!r}"
                )
        return cls(
            request_id=payload["request_id"],
            chars_requested=payload["chars_requested"],
            utterance_id=payload["utterance_id"],
            replay_text=payload["replay_text"],
            editor_generation=payload["editor_generation"],
            # Absent on old-producer payloads -> counted mode (wh-uf54
            # graceful degradation).
            whole_utterance=payload.get("whole_utterance", False),
        )


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetractEditorTextResponse:
    """GUI -> Logic response payload for retract-and-replay.

    Fields:
      * ``request_id`` -- echoes the request id; Logic uses it to
        complete the awaiting future.
      * ``chars_requested`` -- echoes the request value. Lets Logic
        validate at the boundary (round 1 / codex finding A,
        wh-g2-refactor.5.1).
      * ``chars_removed`` -- non-negative int. The grapheme-cluster
        count the GUI actually removed. MUST equal ``chars_requested``
        on the success path (round 1 / codex finding C,
        wh-g2-refactor.5.3).
      * ``replay_chars`` -- non-negative int. The UTF-16 code-unit count
        the GUI inserted on the replay side (0 on the retract-only
        path).
      * ``failure_reason`` -- one of the enumerated reasons, with
        ``""`` meaning success.
    """

    request_id: str
    chars_requested: int
    chars_removed: int
    replay_chars: int
    failure_reason: str
    # Echo of the request's whole_utterance flag. In whole-utterance
    # mode the success invariant chars_removed == chars_requested does
    # NOT apply: the GUI removed the ledger's true total, and the
    # advisory chars_requested diverging from it is the mirror drift
    # the mode exists to survive.
    whole_utterance: bool = False

    def __post_init__(self) -> None:
        _check_str("request_id", self.request_id, allow_empty=False)
        if not isinstance(self.whole_utterance, bool):
            raise RetractEditorTextSchemaError(
                "whole_utterance must be a bool, got "
                f"{type(self.whole_utterance).__name__}"
            )
        # chars_requested is non-negative on the response. The fan-out
        # branch on rebuild may carry chars_requested == -1 from the
        # synthesised editor_rebuilt failure (Section 6), but the
        # well-formed wire response always echoes the request's positive
        # value or the abandon-path sentinel. We allow >= -1 to model
        # both cases; producers should still echo the request's
        # chars_requested on every GUI-side path.
        _check_int("chars_requested", self.chars_requested, minimum=-1)
        _check_int("chars_removed", self.chars_removed, minimum=0)
        _check_int("replay_chars", self.replay_chars, minimum=0)
        _check_str("failure_reason", self.failure_reason, allow_empty=True)
        if self.failure_reason not in ALLOWED_FAILURE_REASONS:
            raise RetractEditorTextSchemaError(
                f"failure_reason {self.failure_reason!r} is not allowed; "
                f"must be one of {sorted(ALLOWED_FAILURE_REASONS)!r}"
            )
        # Round 1 / codex finding C, wh-g2-refactor.5.3: success path
        # requires chars_removed == chars_requested. The validator
        # enforces this at the boundary so the speech processor's
        # success-branch decision (skip replay) is structurally safe.
        # Counted mode only: in whole-utterance mode chars_requested is
        # advisory and chars_removed is the ledger's true total.
        if self.failure_reason == FAILURE_SUCCESS and not self.whole_utterance:
            if self.chars_removed != self.chars_requested:
                raise RetractEditorTextSchemaError(
                    "success response requires chars_removed == "
                    f"chars_requested (sent={self.chars_requested}, "
                    f"got={self.chars_removed})"
                )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict."""
        return {
            "action": ACTION_NAME_RESPONSE,
            "request_id": self.request_id,
            "chars_requested": self.chars_requested,
            "chars_removed": self.chars_removed,
            "replay_chars": self.replay_chars,
            "failure_reason": self.failure_reason,
            "whole_utterance": self.whole_utterance,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "RetractEditorTextResponse":
        """Parse and validate a wire-format response dict."""
        if not isinstance(payload, Mapping):
            raise RetractEditorTextSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        action = payload.get("action")
        if action != ACTION_NAME_RESPONSE:
            raise RetractEditorTextSchemaError(
                f"action {action!r} does not match {ACTION_NAME_RESPONSE!r}"
            )
        for required in (
            "request_id",
            "chars_requested",
            "chars_removed",
            "replay_chars",
            "failure_reason",
        ):
            if required not in payload:
                raise RetractEditorTextSchemaError(
                    f"payload missing required field {required!r}"
                )
        return cls(
            request_id=payload["request_id"],
            chars_requested=payload["chars_requested"],
            chars_removed=payload["chars_removed"],
            replay_chars=payload["replay_chars"],
            failure_reason=payload["failure_reason"],
            whole_utterance=payload.get("whole_utterance", False),
        )
