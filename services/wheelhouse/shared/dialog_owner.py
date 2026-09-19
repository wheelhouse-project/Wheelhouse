"""Owner tokens for the one working dialog every operation shares.

One dialog serves four kinds of operation: a provider start driven by
RemoteSTTLauncher, the WebSocket completion that ends that same start,
an AI request, and the GUI's own raise while the app is starting. Any of
them can address the dialog while another owns it, so a message that
shows or dismisses it has to say which operation it belongs to
(wh-dialog-ownership-token, wh-launch-addressed-notices).

The token is a string ``"<source>:<id>"``. The source prefix is what
makes a collision between the sources impossible, and a bare integer
could not: two integer counters already exist and can diverge --
``RemoteSTTLauncher._current_launch_generation`` and
``WebSocketManager._stt_client_generations`` -- so launch 2 and AI
request 2 would be one value. Prefixing makes non-collision structural
rather than a matter of the two counters happening to disagree.

The launcher keeps its own launch generation as an integer. That integer
is what ``launch_is_current``, ``signal_provider_ready`` and the
per-connection stamps compare, and changing it would reach far past this
fix; only the token handed to the dialog is built from it, at the point
where the dialog is addressed.

``shared.correlation_token`` was checked first and does not fit: it
validates uuid4 strings for the text-target privacy contract, and an
opaque uuid would lose the source prefix that non-collision rests on.
"""

from __future__ import annotations

import uuid
from typing import Optional

#: The GUI's raise while the app starts. It is a placeholder rather than
#: an operation: nothing can outlive startup, and what ends startup does
#: not always know a launch token -- the WebSocket ready path sends no
#: token whenever the connection carries no launch stamp. So a dismiss
#: that names no operation still takes this dialog down. See
#: ``WorkingDialog.hide_working``.
STARTUP_OWNER = "gui:startup"


def launch_owner_token(generation: Optional[int]) -> Optional[str]:
    """The owner token naming one provider launch.

    Args:
        generation: A launch generation, or None when the caller has no
            launch to name.

    Returns:
        ``"stt:<generation>"``, or None for None. None means "this
        message names no operation", which is what an unstamped sender
        has always meant.
    """
    if generation is None:
        return None
    return f"stt:{generation}"


def next_ai_owner_token() -> str:
    """The owner token naming the AI request starting now.

    Each call answers a token no earlier call answered, so an AI request
    that finishes late cannot dismiss a later request's dialog.

    A uuid4 rather than a counter, and not for uniqueness alone. main.py
    imports in-package modules as ``services.wheelhouse.<x>`` while
    gui.py, websocket_manager.py and speech/actions.py import them bare.
    Both resolve, so this module can be loaded twice under two names --
    and two copies of a module-global counter would each start at 1 and
    hand the same "ai:1" to two different requests, which is the
    collision A1 forbids. A uuid4 keeps no shared state, so duplicate
    module objects cannot collide. ``launch_owner_token`` and
    ``STARTUP_OWNER`` are safe under the same duplication because a pure
    function and a constant give equal values from either copy.

    Returns:
        ``"ai:<uuid4>"``.
    """
    return f"ai:{uuid.uuid4()}"
