"""text_target_grant_prompt IPC event schema (wh-bqv9c).

Defines the Logic -> GUI event the click counter forwards when a
tuple's verified-retry counter reaches the soft-allow threshold. The
GUI uses the payload to render the three-strikes follow-up toast
("Always type into <App> when you do this?"). The toast is per-tuple
per-session deduped on the GUI side; if the user dismisses without
clicking Yes or No, the next ``RetryThresholdReached`` event re-fires
the toast.

Transport: ``LogicController._on_retry_threshold_reached`` puts a dict
produced by ``TextTargetGrantPromptEvent.to_dict()`` onto the existing
``state_to_gui_queue`` after replacing the ``"type"`` key with
``"action"``. The GUI's queue listener routes by ``action`` and calls
``_show_grant_prompt_toast``, which validates with
``TextTargetGrantPromptEvent.from_dict`` and renders the widget.

Privacy contract: this event carries the platform identity triple,
the application's friendly name, and the per-tuple count value. No
dictation text, no correlation_token, no user content. The fields
match the ``RejectionTuple`` cache and the soft-allow persistence
file format. The count is a counter value, not user data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


MSG_TYPE = "text_target_grant_prompt"


class TextTargetGrantPromptSchemaError(ValueError):
    """Raised by ``TextTargetGrantPromptEvent.from_dict`` on a malformed payload.

    The forwarder should catch this via ``safe_parse`` (wh-uf54) and
    degrade gracefully (log + drop). A schema error must never crash
    the GUI loop.
    """


@dataclass(frozen=True)
class TextTargetGrantPromptEvent:
    """Structured payload of a text_target_grant_prompt Logic -> GUI event."""

    process_name: str
    class_name: str
    control_type: str
    app_friendly_name: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The returned dict has a ``"type"`` key carrying ``MSG_TYPE``.
        The Logic-side forwarder rebrands this to ``"action"`` before
        posting to the GUI state queue (the queue listener dispatches
        by ``"action"``); the schema's own dict carries ``"type"`` so
        the schema is symmetric with the input -> logic schemas in
        :mod:`shared.text_target_rejection`.
        """

        return {
            "type": MSG_TYPE,
            "process_name": self.process_name,
            "class_name": self.class_name,
            "control_type": self.control_type,
            "app_friendly_name": self.app_friendly_name,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "TextTargetGrantPromptEvent":
        """Parse and validate a wire-format dict.

        Raises ``TextTargetGrantPromptSchemaError`` on any structural
        problem: not a mapping, missing or wrong ``"type"``, missing
        field, wrong field type, ``count`` that is not a positive int,
        or ``count`` that is a bool (which would otherwise satisfy
        ``isinstance(value, int)`` because ``bool`` subclasses ``int``).
        """

        if not isinstance(payload, Mapping):
            raise TextTargetGrantPromptSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "type" not in payload:
            raise TextTargetGrantPromptSchemaError(
                "payload missing required key 'type'"
            )
        if payload["type"] != MSG_TYPE:
            raise TextTargetGrantPromptSchemaError(
                f"payload type {payload['type']!r} does not match {MSG_TYPE!r}"
            )

        string_fields = (
            "process_name",
            "class_name",
            "control_type",
            "app_friendly_name",
        )
        for name in string_fields:
            if name not in payload:
                raise TextTargetGrantPromptSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if not isinstance(value, str):
                raise TextTargetGrantPromptSchemaError(
                    f"field {name!r} must be a str, got {type(value).__name__}"
                )

        if "count" not in payload:
            raise TextTargetGrantPromptSchemaError(
                "payload missing required field 'count'"
            )
        raw_count = payload["count"]
        if isinstance(raw_count, bool) or not isinstance(raw_count, int):
            raise TextTargetGrantPromptSchemaError(
                "field 'count' must be an int, got "
                f"{type(raw_count).__name__}"
            )
        if raw_count < 1:
            raise TextTargetGrantPromptSchemaError(
                f"field 'count' must be >= 1, got {raw_count}"
            )

        return cls(
            process_name=payload["process_name"],
            class_name=payload["class_name"],
            control_type=payload["control_type"],
            app_friendly_name=payload["app_friendly_name"],
            count=raw_count,
        )
