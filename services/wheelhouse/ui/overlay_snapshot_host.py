"""The UIActionHandler members that OverlaySnapshotActions uses.

OverlaySnapshotActions keeps a back-reference to the UIActionHandler that built
it and reads these members through it. They stay on UIActionHandler because
click by name (click_element) and the rest of the handler also use them: the
response queue, the shadow buffer, the validated [click] config, the
automation root and the lazily built finder and executor. This Protocol lists
them, so the back-reference is explicit and the type checker sees each use.
"""

from __future__ import annotations

from multiprocessing import Queue
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    from .click_config import ClickConfig
    from .click_executor import ClickExecutor
    from .element_finder import ElementFinder
    from .shadow_buffer import ShadowBufferManager


class OverlaySnapshotHost(Protocol):
    """The members of UIActionHandler that OverlaySnapshotActions reads."""

    response_queue: Queue
    buffer_manager: ShadowBufferManager

    # None until get_click_element_finder or get_click_executor has run
    # one time: each of them sets the config as a side effect.
    @property
    def click_config(self) -> Optional[ClickConfig]: ...

    # True when the UI Automation root failed to build on this host.
    def click_automation_unavailable(self) -> bool: ...

    # None when voice clicking is disabled by config, or when the UI
    # Automation root cannot be built on this host.
    def get_click_element_finder(self) -> Optional[ElementFinder]: ...

    def get_click_executor(self) -> ClickExecutor: ...
