"""Focus-redirect integration -- shrunk stub (wh-g2-refactor.18).

Historical note: this module used to host the FocusRedirectPath class
that wired the focus-redirect policy, the focus-change word buffer,
and the editor lifecycle state machine into the speech pipeline. The
class was 2046 lines of held-back queue management, asymmetric
callback wiring, drain coordination, and per-utterance ordering
invariants. All of that machinery existed because the dictation
editor was created on demand and the pipeline had to wait for the
QPlainTextEdit to exist and for Windows foreground to transfer to it
before SendInput could safely type words.

The G2 refactor replaced the on-demand editor with a persistent
hidden editor that exists at GUI startup. Words insert via direct Qt
calls (QTextCursor.insertText) instead of SendInput, which removes
the need for foreground and removes the entire held-back-queue
contract.

What remains here is the small policy-hook surface and a session-reset
shim that the rest of the speech pipeline still calls during teardown.
The decide-when-to-show logic lives in ``focus_redirect_policy.py``,
which is unchanged. The IPC plumbing (``insert_editor_word`` /
``retract_editor_text``) is owned by ``main.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


__all__ = ["reset_focus_redirect_session"]


def reset_focus_redirect_session() -> None:
    """Reset the focus-redirect session state.

    With the persistent hidden editor (G2), the only durable
    session-scoped state was the policy's per-utterance prompt-detector
    cache. That cache is reset by
    ``FocusRedirectPolicy.on_utterance_end`` directly, which the
    SpeechProcessor's utterance-end handler invokes when the policy
    is wired into it. This shim is provided as the named seam the rest
    of the speech pipeline calls so a future re-introduction of any
    larger reset surface has a single hook to extend.
    """
    return None
