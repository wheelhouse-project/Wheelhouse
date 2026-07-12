"""try_anyway_clicked GUI -> Logic event schema (wh-iycks).

Defines the GUI-to-Logic event the GUI manager emits when the user
clicks the "Try it anyway" button on a rejection toast (wh-z7qx1).
The payload carries only the ``correlation_token`` from the rejection
that produced the toast; the dictation text never crosses processes
under this contract (wh-x4mv.2 round-2 privacy split).

Transport: GuiManager.send_command puts a dict produced by
``TryAnywayClickedEvent.to_dict()`` -- shape ``{"action": "try_anyway_clicked",
"correlation_token": "<uuid4>"}`` -- onto the existing
commands_to_logic_queue. ``LogicController._listen_for_gui_commands``
routes the action via its handler_map; the handler validates the
schema with :func:`safe_parse` (wh-uf54) so a malformed payload is
logged and dropped rather than crashing the GUI command listener.

The action key matches the dataclass intent: there is exactly one
event kind on this channel per click, and the schema validation
fence catches version-skew payload shapes at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from services.wheelhouse.shared.correlation_token import (
    validate_correlation_token,
)


ACTION_NAME = "try_anyway_clicked"


class TryAnywayClickedSchemaError(ValueError):
    """Raised by ``TryAnywayClickedEvent.from_dict`` on a malformed payload.

    The Logic handler should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54.
    """


@dataclass(frozen=True)
class TryAnywayClickedEvent:
    """Structured payload of a try_anyway_clicked GUI -> Logic event."""

    correlation_token: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The returned dict is the action payload that GuiManager.send_command
        puts onto commands_to_logic_queue; the ``"action"`` key carries
        ``ACTION_NAME`` so the existing dispatch in
        ``_listen_for_gui_commands`` can route it.
        """

        return {
            "action": ACTION_NAME,
            "correlation_token": self.correlation_token,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "TryAnywayClickedEvent":
        """Parse and validate a wire-format dict.

        Raises ``TryAnywayClickedSchemaError`` on any structural
        problem: not a mapping, missing or wrong ``"action"``, missing
        ``"correlation_token"``, or correlation_token that fails the
        uuid4 canonical-form check.
        """

        if not isinstance(payload, Mapping):
            raise TryAnywayClickedSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise TryAnywayClickedSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise TryAnywayClickedSchemaError(
                f"payload action {payload['action']!r} does not match {ACTION_NAME!r}"
            )

        if "correlation_token" not in payload:
            raise TryAnywayClickedSchemaError(
                "payload missing required field 'correlation_token'"
            )
        token = validate_correlation_token(
            payload["correlation_token"],
            field_name="correlation_token",
            error_class=TryAnywayClickedSchemaError,
        )

        return cls(correlation_token=token)
