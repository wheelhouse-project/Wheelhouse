"""grant_prompt_yes_clicked GUI -> Logic event schema (wh-8d81z).

When the user clicks Yes on the three-strikes follow-up toast (wh-bqv9c),
the GUI manager emits this event with the identity tuple of the active
grant prompt. The Logic handler:

  1. Calls ``LogicController.add_soft_allow(process, class, control_type)``
     which writes the soft-allow file via the wh-z0usg atomic writer and,
     on disk-write success, sends ``add_soft_allow_tuple`` IPC to input
     (wh-01t75).
  2. On full success, resets the click counter for the tuple so any
     future de-grant by the user does not re-fire the threshold prompt
     immediately on the next retry.
  3. On disk-write failure, ``add_soft_allow`` already emits
     ``soft_allow_write_failed`` to the GUI state queue (wh-9dkse will
     eventually surface a "couldn't save" toast); the counter is NOT
     reset so the user can click Yes again later.

Transport: GuiManager.send_command puts the dict produced by
``GrantPromptYesClickedEvent.to_dict()`` -- shape
``{"action": "grant_prompt_yes_clicked", "process_name": ...,
"class_name": ..., "control_type": ...}`` -- onto
commands_to_logic_queue. ``LogicController._listen_for_gui_commands``
routes the action via its handler_map; the handler validates with
:func:`safe_parse` (wh-uf54) so a malformed payload is logged and
dropped rather than crashing the listener.

Privacy contract: this event carries platform identity only -- the
process name, class name, and control type. No dictation text and no
correlation_token. The toast only surfaces after a verified retry has
already cleared the rejection-cache lookup, and the counter triple is
the same set of fields the rejection predicate already holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ACTION_NAME = "grant_prompt_yes_clicked"


class GrantPromptYesClickedSchemaError(ValueError):
    """Raised by ``GrantPromptYesClickedEvent.from_dict`` on a malformed payload.

    The Logic handler should catch this via ``safe_parse`` and degrade
    gracefully (log + drop), per wh-uf54.
    """


@dataclass(frozen=True)
class GrantPromptYesClickedEvent:
    """Structured payload of a grant_prompt_yes_clicked GUI -> Logic event."""

    process_name: str
    class_name: str
    control_type: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the wire-format dict.

        The returned dict is the action payload that GuiManager.send_command
        puts onto commands_to_logic_queue; the ``"action"`` key carries
        ``ACTION_NAME`` so the existing dispatch in
        ``_listen_for_gui_commands`` can route it.
        """

        return {
            "action": ACTION_NAME,
            "process_name": self.process_name,
            "class_name": self.class_name,
            "control_type": self.control_type,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "GrantPromptYesClickedEvent":
        """Parse and validate a wire-format dict.

        Raises ``GrantPromptYesClickedSchemaError`` on any structural
        problem: not a mapping, missing or wrong ``"action"``, missing
        or non-string identity field.
        """

        if not isinstance(payload, Mapping):
            raise GrantPromptYesClickedSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )

        if "action" not in payload:
            raise GrantPromptYesClickedSchemaError(
                "payload missing required key 'action'"
            )
        if payload["action"] != ACTION_NAME:
            raise GrantPromptYesClickedSchemaError(
                f"payload action {payload['action']!r} does not match "
                f"{ACTION_NAME!r}"
            )

        for name in ("process_name", "class_name", "control_type"):
            if name not in payload:
                raise GrantPromptYesClickedSchemaError(
                    f"payload missing required field {name!r}"
                )
            value = payload[name]
            if not isinstance(value, str):
                raise GrantPromptYesClickedSchemaError(
                    f"field {name!r} must be a str, got "
                    f"{type(value).__name__}"
                )

        return cls(
            process_name=payload["process_name"],
            class_name=payload["class_name"],
            control_type=payload["control_type"],
        )
