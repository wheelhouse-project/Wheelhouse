"""Editor lifecycle minimal reset specification (wh-g2-refactor.18).

Historical note: this module used to host the full lifecycle state
machine (407 lines) for the on-demand dictation editor: the
EditorState enum with seven non-terminal/recovery states, the
EditorEventKind enum, the per-state timeout table, the transition
table, the EditorLifecycleEvent dataclass, the LogicMirror with its
session/seq enforcement, the MirrorOutcome enum, and the
_CLEAN_SESSION_END_STATES frozenset.

The G2 refactor replaced the on-demand editor with a persistent
hidden editor that exists at GUI startup. The full state machine is
no longer required: the editor is always alive, words insert via
direct Qt calls regardless of foreground state, and the
focus-redirect path that drove the mirror is gone.

What remains is the minimum the focus-redirect policy
(``focus_redirect_policy.py``, "decide when to show") still imports:

  * :class:`EditorState` -- the enum of lifecycle states. The policy
    consults ``_EDITOR_OPEN_STATES`` (a frozenset of values from this
    enum) to answer "is the editor already open?" and consults
    ``EditorState.ERROR`` to fail closed on a recovering editor.
  * :class:`LogicMirror` -- a minimal stub with a ``.state`` attribute
    and a ``reset_to_closed()`` method. With the persistent editor,
    nothing drives the mirror through any state except CLOSED, so
    the policy's "editor already open" check is effectively dead.
    The stub keeps the import surface compatible so existing tests
    and call sites work without rewrite.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class EditorState(enum.Enum):
    """Lifecycle states for the terminal dictation editor.

    wh-g2-refactor.18: the persistent hidden editor sits in CLOSED
    from the policy's point of view. The other values survive purely
    so existing imports (and the policy's _EDITOR_OPEN_STATES
    frozenset) still resolve.
    """

    CLOSED = "closed"
    OPEN_REQUESTED = "open_requested"
    OPEN_APPLIED = "open_applied"
    FOCUS_PENDING = "focus_pending"
    FOCUS_CONFIRMED = "focus_confirmed"
    SUBMITTING = "submitting"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class LogicMirror:
    """Logic-side mirror of the editor state.

    wh-g2-refactor.18 reduced this to a minimal stub. The persistent
    hidden dictation editor does not drive a session lifecycle, so
    the only state the policy reads (``self.state``) is always
    ``CLOSED`` in production. The ``reset_to_closed`` method is
    preserved so any remaining caller has a no-op seam.
    """

    state: EditorState = EditorState.CLOSED

    def reset_to_closed(self) -> None:
        """Reset the mirror state to CLOSED (a no-op for the stub)."""
        self.state = EditorState.CLOSED
