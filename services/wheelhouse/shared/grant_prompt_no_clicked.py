"""grant_prompt_no_clicked GUI -> Logic event schema (wh-vdt1t).

When the user clicks No on the three-strikes follow-up toast (wh-bqv9c),
the GUI manager emits this event with the identity tuple of the active
grant prompt. The Logic handler records the tuple in a per-run
suppression set; subsequent ``RetryThresholdReached`` events for the
same tuple are suppressed at the Logic-side forwarder so the follow-up
toast does not re-fire during the current run, even across a GUI
restart (the resolution to wh-vbvgf.7.1 deferred from the wh-bqv9c
codex review).

The counter itself is intentionally NOT reset on No (per bead spec):
"On No click, logic process leaves the counter alone. It does NOT
reset to 0 or advance to a never-ask state. The next verified retry
increments the counter further (4, 5, 6, ...)".

Transport: GuiManager.send_command puts the dict produced by
``GrantPromptNoClickedEvent.to_dict()`` -- shape
``{"action": "grant_prompt_no_clicked", "process_name": ...,
"class_name": ..., "control_type": ...}`` -- onto
commands_to_logic_queue. ``LogicController._listen_for_gui_commands``
routes the action via its handler_map; the handler validates with
:func:`safe_parse` (wh-uf54) so a malformed payload is logged and
dropped rather than crashing the listener.

Privacy contract: this event carries platform identity only. No
dictation text and no correlation_token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ACTION_NAME = "grant_prompt_no_clicked"


class GrantPromptNoClickedSchemaError(ValueError):
    """Raised by ``GrantPromptNoClickedEvent.from_dict`` on a malformed payload.

    The Logic handler should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54.
    """


@dataclass(frozen=True)
class GrantPromptNoClickedEvent:
    """Structured payload of a grant_prompt_no_clicked GUI -> Logic event."""

    process_name: str
    class_name: str
    control_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": ACTION_NAME,
            "process_name": self.process_name,
            "class_name": self.class_name,
            "control_type": self.control_type,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "GrantPromptNoClickedEvent":
        if not isinstance(payload, Mapping):
            raise GrantPromptNoClickedSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise GrantPromptNoClickedSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise GrantPromptNoClickedSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        for name in ("process_name", "class_name", "control_type"):
            if name not in payload:
                raise GrantPromptNoClickedSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if not isinstance(value, str):
                raise GrantPromptNoClickedSchemaError(
                    f"field {name!r} must be a str, got "
                    f"{type(value).__name__}"
                )

        return cls(
            process_name=payload["process_name"],
            class_name=payload["class_name"],
            control_type=payload["control_type"],
        )
