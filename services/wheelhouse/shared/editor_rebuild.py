"""GUI-side persistent-editor rebuild orchestrator (wh-g2-refactor.17).

Implements the first half of Section 6 of
``docs/design/2026-05-20-g2-refactor-design-refinements.md``:
destroy-and-reconstruct recovery with generation fencing for the
persistent hidden dictation editor.

The actual ``TerminalDictationEditorWindow`` lives in the GUI process
and depends on PySide6; the wiring into ``GuiManager`` is slice 6
(``wh-g2-refactor.18``). This module deliberately encodes the rebuild
*sequence* (bump GuiManager counter, bump OLD editor's counter, emit
``editor_rebuilt`` notification, close/deleteLater, clear the
reference) behind plain callables so the contract is testable without
Qt and reusable from any future Qt-side caller.

The Section 6 sequence the orchestrator enforces:

  1. Snapshot ``old_gen`` from the GuiManager-side counter; bump it to
     ``new_gen = old_gen + 1`` BEFORE anything visible runs. The
     dispatcher's stale-generation check then rejects any queued
     message that gets dequeued before ``deleteLater`` fires.
  2. Also bump the OLD editor's own ``_editor_generation`` to
     ``new_gen`` BEFORE the notification and BEFORE close/deleteLater
     (round 1 / deepseek finding 8.1). The dispatcher's per-request
     check reads the editor's counter, NOT GuiManager's; without this
     bump, a stale request stamped with ``old_gen`` that dequeues
     from ``state_to_gui_queue`` during the window between the
     GuiManager bump and the destroy would pass the check, mutate an
     editor about to be deleted, and silently lose the user's word.
  3. Enqueue the ``editor_rebuilt`` notification onto
     ``commands_to_logic_queue`` so Logic can fan out failures to
     pending futures.
  4. Clear the GuiManager's editor reference (``set_editor(None)``).
  5. Call ``close()`` then ``deleteLater()`` on the OLD editor. Both
     calls are wrapped in try/except per the design pseudocode --
     a raise from either does NOT abort the rebuild; the orchestrator
     logs and continues.

Lazy reconstruction is deliberately NOT part of this module: the
design specifies that the next ``open_te`` request from Logic
constructs the fresh editor on demand, not the rebuild handler. That
gives Qt the typical "next paint event recovers the device" window
deepseek called out for the Modern-Standby resume case.

``RebuildLost`` is a convenience exception type for two audiences:

1. **Test code** that wants to inspect a fan-out payload as a
   structured exception with cleaner assertions than dict access.
2. **Logic-side callers that already have generation context** (the
   editor_rebuilt notification handler in
   ``editor_rebuilt_handler.py`` knows ``old_generation``,
   ``new_generation``, and ``reason`` from the notification; it can
   attach those to a ``RebuildLost`` if a future caller wants richer
   logging).

Production producers (the post-await branches in ``main.py`` for
``insert_editor_word`` and ``retract_editor_text``) do NOT raise
``RebuildLost``. They branch on ``failure_reason`` directly per
Section 2 and Section 5 of the design doc, or use
``LogicRebuildFanout.is_rebuild_lost(payload)`` to gate the case.
The synthetic IPC payload travels as a dict (futures resolve with
dicts, not exceptions), and ``RebuildLost`` is sugar on top of that
dict rather than a replacement for it.

``RebuildLost.from_payload`` returns an instance when the payload's
``failure_reason`` matches a rebuild fence. The ``old_generation`` /
``new_generation`` / ``reason`` kwargs default to sentinels because
the synthesised fan-out payload is universal across every stale
future and does not carry per-future context -- the kwargs are
optional caller-supplied context to attach when the caller has it
(typically only the Logic-side fan-out caller does).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional, Protocol

from services.wheelhouse.shared.editor_rebuilt import (
    ACTION_NAME as EDITOR_REBUILT_ACTION,
    EditorRebuiltNotification,
)
from services.wheelhouse.shared.insert_editor_word import (
    FAILURE_EDITOR_REBUILT as INSERT_FAILURE_EDITOR_REBUILT,
    FAILURE_STALE_GENERATION as INSERT_FAILURE_STALE_GENERATION,
)
from services.wheelhouse.shared.retract_editor_text import (
    FAILURE_EDITOR_REBUILT as RETRACT_FAILURE_EDITOR_REBUILT,
)


logger = logging.getLogger(__name__)


# The two schemas use the same constant value for the editor_rebuilt
# failure reason, but assert it explicitly here so the rebuild
# orchestrator does not silently misalign if either schema changes
# the spelling in isolation.
assert INSERT_FAILURE_EDITOR_REBUILT == RETRACT_FAILURE_EDITOR_REBUILT == "editor_rebuilt"


class RebuildLost(Exception):
    """A pending IPC future was abandoned by the rebuild fan-out.

    Carries the ``old_generation`` (the editor generation the request
    was issued under), the ``new_generation`` (the editor the next
    request will go to), and the ``reason`` string the rebuild logged.

    Audience: test code and Logic-side callers that have generation
    context. Production producers in ``main.py`` do NOT raise this --
    they branch on ``failure_reason`` directly. See the module
    docstring for the full rationale.

    ``failure_reason`` here distinguishes the two semantically-equivalent
    but log-distinct rebuild fences (Section 6 IPC schema table):

      * ``"editor_rebuilt"`` -- the request was abandoned in bulk by
        the Logic-side fan-out when the ``editor_rebuilt`` notification
        arrived. This is the canonical RebuildLost case.
      * ``"stale_generation"`` -- the request was issued under an old
        generation and the GUI dispatcher rejected it locally. Less
        common (the fan-out usually fires first) but still a "future
        resolved with no editor mutation" outcome.
    """

    def __init__(
        self,
        *,
        old_generation: int,
        new_generation: int,
        reason: str,
        failure_reason: str = INSERT_FAILURE_EDITOR_REBUILT,
    ) -> None:
        self.old_generation = old_generation
        self.new_generation = new_generation
        self.reason = reason
        self.failure_reason = failure_reason
        super().__init__(
            f"editor rebuild discarded request "
            f"(old_gen={old_generation}, new_gen={new_generation}, "
            f"reason={reason!r}, failure_reason={failure_reason!r})"
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        old_generation: int = -1,
        new_generation: int = -1,
        reason: str = "",
    ) -> Optional["RebuildLost"]:
        """Return a RebuildLost if the payload carries a rebuild-fence reason.

        Returns ``None`` when ``payload["failure_reason"]`` is anything
        other than ``"editor_rebuilt"`` or ``"stale_generation"``. The
        producer's post-await branch can therefore call this and let
        non-rebuild responses fall through unchanged.

        ``old_generation`` / ``new_generation`` / ``reason`` default to
        sentinels because the synthesised fan-out payload does not
        carry them -- the fan-out caller knows the generations but the
        payload itself is universal across all stale futures. Pass
        them explicitly when the caller has the context.
        """
        if not isinstance(payload, Mapping):
            return None
        reason_str = payload.get("failure_reason")
        if reason_str not in (
            INSERT_FAILURE_EDITOR_REBUILT,
            INSERT_FAILURE_STALE_GENERATION,
        ):
            return None
        return cls(
            old_generation=old_generation,
            new_generation=new_generation,
            reason=reason,
            failure_reason=str(reason_str),
        )


class _EditorLike(Protocol):
    """Protocol for the OLD editor instance the orchestrator destroys.

    The orchestrator reads/writes ``_editor_generation`` and calls
    ``close`` and ``deleteLater``. ``TerminalDictationEditorWindow``
    satisfies this protocol; the unit tests stub it with a plain
    dataclass.
    """

    _editor_generation: int

    def close(self) -> Any: ...
    def deleteLater(self) -> Any: ...


class PersistentEditorRebuilder:
    """Orchestrate the GUI-side destroy-and-reconstruct rebuild.

    The class is Qt-free; all interaction with GuiManager state lives
    behind callables passed at construction. ``rebuild(reason)`` is
    the entry point: it bumps the generations, posts the
    ``editor_rebuilt`` notification, clears the editor reference, and
    closes the OLD editor. Returns the new generation so the caller
    can re-stamp any in-flight Logic-side bookkeeping.

    Parameters
    ----------
    get_editor :
        Zero-arg callable returning the live editor (or ``None`` if
        none is currently constructed -- the rebuild can still fire to
        bump the GuiManager-side counter and emit the notification).
        Mirrors the ``EditorIpcResponder.get_editor`` shape.
    set_editor :
        Single-arg callable that sets the GuiManager's editor
        reference. Called with ``None`` after the bumps but before the
        ``close`` / ``deleteLater`` so the Section 6 invariant
        ``self._te_window = None`` runs at the right place in the
        sequence.
    get_generation :
        Zero-arg callable returning the GuiManager's current
        ``self._editor_generation``. The orchestrator never holds the
        counter itself; the counter lives on GuiManager so the lazy
        reconstruction in the next ``open_te`` handler reads the
        same source of truth.
    set_generation :
        Single-arg callable that writes the new generation back to
        GuiManager's ``self._editor_generation``.
    post_notification :
        Single-arg callable that enqueues the ``editor_rebuilt``
        dict onto ``commands_to_logic_queue``. In production this is
        a thin wrapper around ``queue.put_nowait``; in tests it is a
        list ``.append`` so the test can assert ordering.

    Class is NOT thread-safe; it must be driven from the Qt main
    thread (the same thread the host dispatcher runs on).
    """

    def __init__(
        self,
        *,
        get_editor: Callable[[], Optional[_EditorLike]],
        set_editor: Callable[[Optional[_EditorLike]], None],
        get_generation: Callable[[], int],
        set_generation: Callable[[int], None],
        post_notification: Callable[[Mapping[str, Any]], None],
    ) -> None:
        self._get_editor = get_editor
        self._set_editor = set_editor
        self._get_generation = get_generation
        self._set_generation = set_generation
        self._post_notification = post_notification

    def rebuild(self, reason: str) -> int:
        """Run the rebuild sequence; return the new generation.

        The sequence follows Section 6's pseudocode exactly. The
        method is idempotent in the sense that calling it twice in
        quick succession bumps the generation twice and emits two
        notifications -- neither call corrupts state, but the second
        call's "old editor" is whatever the first call left behind
        (typically ``None``).

        ``reason`` is logged at WARNING level (matches the
        pseudocode's ``logger.warning(...)`` opener) and propagated
        verbatim onto the notification.
        """
        logger.warning("rebuilding persistent dictation editor: %s", reason)

        # Round 2 / codex finding 7.5: bump the generation BEFORE the
        # destroy. The dispatcher's stale-generation check then
        # rejects any queued message that gets dequeued before
        # deleteLater fires.
        old_gen = int(self._get_generation())
        new_gen = old_gen + 1
        self._set_generation(new_gen)

        # Round 1 / deepseek finding 8.1: ALSO bump the OLD editor's
        # own _editor_generation to new_gen BEFORE the editor_rebuilt
        # notification and BEFORE close()/deleteLater(). The GUI
        # dispatcher's check at the per-request branch reads the
        # editor's counter, NOT GuiManager's. Without this bump, a
        # stale request stamped with old_gen that dequeues from
        # state_to_gui_queue during the window between the GuiManager
        # bump and the close()/deleteLater() actually destroying the
        # editor would pass the check, mutate an editor about to be
        # destroyed, and silently lose the user's word. Mutating
        # _editor_generation on an editor that is about to be deleted
        # is harmless.
        old = self._get_editor()
        if old is not None:
            try:
                old._editor_generation = new_gen
            except Exception:  # noqa: BLE001
                # A protocol-violating stub (no settable attribute)
                # would land here; in production the editor's
                # _editor_generation is a plain int attribute and the
                # assignment cannot raise.
                logger.exception(
                    "bumping old editor _editor_generation raised; continuing",
                )

        # Build and post the editor_rebuilt notification. The
        # EditorRebuiltNotification validator enforces
        # new_gen > old_gen and non-negativity; a malformed call here
        # would surface as a ValueError at construction (programmer
        # error in this module, not a degradation case).
        try:
            notification = EditorRebuiltNotification(
                old_generation=old_gen,
                new_generation=new_gen,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            # Defensive: log and continue with a raw dict on the
            # vanishingly unlikely path where reason is somehow not a
            # str despite the type hint. Validator failure here would
            # indicate a calling-code bug; logging is preferable to
            # silently dropping the notification.
            logger.exception(
                "EditorRebuiltNotification validation failed for "
                "(old=%d, new=%d, reason=%r); posting raw dict",
                old_gen, new_gen, reason,
            )
            notification_payload: dict[str, Any] = {
                "action": EDITOR_REBUILT_ACTION,
                "old_generation": old_gen,
                "new_generation": new_gen,
                "reason": str(reason) if reason is not None else "",
            }
        else:
            notification_payload = notification.to_dict()

        try:
            self._post_notification(notification_payload)
        except Exception:  # noqa: BLE001
            # Matches the design pseudocode's "Failed to enqueue
            # editor_rebuilt notification" branch: log and continue.
            # The Logic-side fan-out is an optimisation; the
            # per-request dispatcher fence is the ground truth, so a
            # missed notification only means stale futures wait for
            # their individual timeouts rather than failing fast.
            logger.warning(
                "Failed to enqueue editor_rebuilt notification "
                "(old=%d, new=%d, reason=%r)",
                old_gen, new_gen, reason,
            )

        # Clear the GuiManager-side editor reference BEFORE close()
        # / deleteLater() so any code that reads the reference during
        # the destroy sees None.
        self._set_editor(None)

        # Close and schedule deletion of the OLD editor. Both calls
        # are wrapped: the design pseudocode says a raise from
        # either MUST NOT abort the rebuild. logger.exception so the
        # operator sees the stack but the rebuild completes.
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                logger.exception("close on old editor raised; continuing")
            try:
                old.deleteLater()
            except Exception:  # noqa: BLE001
                logger.exception("deleteLater on old editor raised; continuing")

        return new_gen
