"""The UIActionHandler members that MousePointerActions uses.

MousePointerActions keeps a back-reference to the UIActionHandler that built
it and reads these members through it. They stay on UIActionHandler because
other UIActionHandler code also uses them: get_validated_click_config fills
the same [click] config cache (_click_config) that the by-name click and the
overlay read, every action puts its reply on the response queue, and the
text actions use the shadow buffer. This Protocol lists them, so the back-reference is
explicit and the type checker sees each use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .click_config import ClickConfig
    from .shadow_buffer import ShadowBufferManager


class MousePointerResponseQueue(Protocol):
    """The one member of the response queue that MousePointerActions calls.

    The Any value of the dict: the reply is a MouseActionResponse dict
    (shared/mouse_action.py, to_dict declares dict[str, Any]).
    """

    def put(self, obj: dict[str, Any]) -> None: ...


class MousePointerHost(Protocol):
    """The members of UIActionHandler that MousePointerActions reads."""

    buffer_manager: ShadowBufferManager
    response_queue: MousePointerResponseQueue

    def get_validated_click_config(self) -> ClickConfig: ...
