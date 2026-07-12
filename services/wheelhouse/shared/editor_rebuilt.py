"""editor_rebuilt notification schema (wh-g2-refactor.14).

Defines the GUI -> Logic notification that fires when the GUI Process
destroys and reconstructs the persistent dictation editor (e.g. after
a foreground-transfer failure or focus-confirmed-poll exhaustion).
Logic receives the notification, updates its observed generation
counter, and fans out ``editor_rebuilt`` failures to every pending
``insert_editor_word`` / ``retract_editor_text`` future whose stored
generation is less than or equal to the retired generation. Section 6
of ``docs/design/2026-05-20-g2-refactor-design-refinements.md`` is the
authoritative reference.

The notification rides the same ``commands_to_logic_queue`` channel as
``te_event_ack``, ``retract_editor_text_response``, and
``insert_editor_word_response``; no new IPC channel is added.

Schema invariants:
  * ``new_generation > old_generation``. A rebuild always advances the
    counter; same or smaller values are malformed.
  * Both generation values are non-negative ints.
  * ``reason`` is a string (may be empty). Carries the same reason
    string the GUI's ``_rebuild_persistent_editor`` call logged so log
    surfaces can correlate the rebuild with the triggering Windows
    event (UAC, fast user switch, RDP, Modern Standby).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ACTION_NAME = "editor_rebuilt"


class EditorRebuiltSchemaError(ValueError):
    """Raised on a malformed editor_rebuilt notification.

    Consumers should catch this and degrade gracefully (log + drop)
    per wh-uf54 (IPC schema validation and graceful degradation).
    """


def _check_int(name: str, value: Any, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EditorRebuiltSchemaError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    if value < minimum:
        raise EditorRebuiltSchemaError(
            f"{name} must be >= {minimum}, got {value}"
        )


@dataclass(frozen=True)
class EditorRebuiltNotification:
    """GUI -> Logic notification that the persistent editor was rebuilt.

    Fields:
      * ``old_generation`` -- the generation that was just retired
        (non-negative int).
      * ``new_generation`` -- the generation the next editor will use
        (must equal ``old_generation + 1``; see the contract note
        below).
      * ``reason`` -- string carrying the triggering reason for log
        correlation.

    Round 1 / deepseek finding wh-g2-refactor.30.2: the validator
    enforces ``new_generation == old_generation + 1``, not the looser
    ``new_generation > old_generation``. Section 6's pseudocode and
    the production rebuild path
    (``editor_rebuild.PersistentEditorRebuilder.rebuild``) both
    compute ``new_gen = old_gen + 1``, so every legitimate
    notification carries that exact relationship. A generation gap
    (e.g. ``old=0, new=2``) is a GUI-side bug -- a double-bump where
    the first notification was lost in flight -- and would leave any
    future at the skipped intermediate generation stranded
    (``fail_at_or_below(old=0)`` does not reach generation 1, and no
    subsequent notification will ever carry ``old=1``). Per the
    wh-uf54 IPC validation philosophy, the boundary check fails loud
    on the gap rather than propagating corrupt state.
    """

    old_generation: int
    new_generation: int
    reason: str

    def __post_init__(self) -> None:
        _check_int("old_generation", self.old_generation, minimum=0)
        _check_int("new_generation", self.new_generation, minimum=0)
        if self.new_generation != self.old_generation + 1:
            raise EditorRebuiltSchemaError(
                "new_generation must equal old_generation + 1 "
                f"(old={self.old_generation}, new={self.new_generation})"
            )
        if not isinstance(self.reason, str):
            raise EditorRebuiltSchemaError(
                f"reason must be a str, got {type(self.reason).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": ACTION_NAME,
            "old_generation": self.old_generation,
            "new_generation": self.new_generation,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "EditorRebuiltNotification":
        if not isinstance(payload, Mapping):
            raise EditorRebuiltSchemaError(
                f"payload must be a mapping, got {type(payload).__name__}"
            )
        action = payload.get("action")
        if action != ACTION_NAME:
            raise EditorRebuiltSchemaError(
                f"action {action!r} does not match {ACTION_NAME!r}"
            )
        for required in ("old_generation", "new_generation", "reason"):
            if required not in payload:
                raise EditorRebuiltSchemaError(
                    f"payload missing required field {required!r}"
                )
        return cls(
            old_generation=payload["old_generation"],
            new_generation=payload["new_generation"],
            reason=payload["reason"],
        )
