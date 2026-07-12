"""Tests for PersistentEditorRebuilder and RebuildLost (wh-g2-refactor.17).

Covers the GUI-side rebuild orchestrator that destroys the existing
persistent editor, bumps the generation counter on both the GuiManager
side and the OLD editor's own attribute (round 1 / deepseek 8.1 and
round 2 / codex 7.5), emits the ``editor_rebuilt`` notification onto
``commands_to_logic_queue``, and clears the editor reference. Lazy
reconstruction in the next ``open_te`` handler is out of scope for
this slice; the orchestrator's contract ends after the OLD editor is
destroyed.

The tests stub the editor with a ``_FakeEditor`` dataclass and stub
GuiManager state with plain dict/list collections so the contract
runs without Qt.

Coverage:
  * Generation bumps fire on both sides (GuiManager counter AND OLD
    editor) BEFORE the notification fires.
  * ``editor_rebuilt`` notification is enqueued with the correct
    payload shape (round-trip through
    ``EditorRebuiltNotification.from_dict``).
  * Editor reference is cleared (``set_editor(None)``) and OLD
    editor's ``close()`` + ``deleteLater()`` are called in order.
  * Notification posting failure does NOT abort the rebuild
    (matches the design pseudocode's ``logger.warning`` branch).
  * Editor ``close()`` / ``deleteLater()`` raises do NOT abort the
    rebuild.
  * Calling ``rebuild`` when no live editor exists still bumps the
    generation and posts the notification.
  * ``RebuildLost.from_payload`` returns an instance for fan-out
    payloads carrying ``editor_rebuilt`` or ``stale_generation``,
    and ``None`` otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from services.wheelhouse.shared.editor_rebuild import (
    PersistentEditorRebuilder,
    RebuildLost,
)
from services.wheelhouse.shared.editor_rebuilt import (
    ACTION_NAME as EDITOR_REBUILT_ACTION,
    EditorRebuiltNotification,
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeEditor:
    """Stand-in for ``TerminalDictationEditorWindow``.

    Only the attributes the orchestrator touches:
      * ``_editor_generation`` (the rebuild bumps this).
      * ``close`` / ``deleteLater`` methods (the rebuild calls both).

    Records the order in which the two methods are invoked so the
    test can assert the design's ordering.
    """

    _editor_generation: int = 0
    close_called: bool = False
    delete_later_called: bool = False
    close_raises: BaseException | None = None
    delete_later_raises: BaseException | None = None
    method_order: list[str] = field(default_factory=list)
    generation_at_close: int = -1
    generation_at_delete_later: int = -1

    def close(self):
        self.close_called = True
        self.method_order.append("close")
        self.generation_at_close = self._editor_generation
        if self.close_raises is not None:
            raise self.close_raises

    def deleteLater(self):
        self.delete_later_called = True
        self.method_order.append("deleteLater")
        self.generation_at_delete_later = self._editor_generation
        if self.delete_later_raises is not None:
            raise self.delete_later_raises


@dataclass
class _GuiState:
    """Stand-in for the GuiManager fields the orchestrator reads/writes."""

    editor: Optional[_FakeEditor] = None
    generation: int = 0
    posted: list[dict] = field(default_factory=list)
    posts_raise: BaseException | None = None

    def get_editor(self) -> Optional[_FakeEditor]:
        return self.editor

    def set_editor(self, value: Optional[_FakeEditor]) -> None:
        self.editor = value

    def get_generation(self) -> int:
        return self.generation

    def set_generation(self, value: int) -> None:
        self.generation = value

    def post(self, payload: dict) -> None:
        if self.posts_raise is not None:
            raise self.posts_raise
        # Copy so the test can mutate the source dict without
        # disturbing the recorded payload.
        self.posted.append(dict(payload))


def _build(state: _GuiState) -> PersistentEditorRebuilder:
    return PersistentEditorRebuilder(
        get_editor=state.get_editor,
        set_editor=state.set_editor,
        get_generation=state.get_generation,
        set_generation=state.set_generation,
        post_notification=state.post,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_rebuild_bumps_both_generation_counters():
    editor = _FakeEditor(_editor_generation=3)
    state = _GuiState(editor=editor, generation=3)
    rebuilder = _build(state)

    new_gen = rebuilder.rebuild(reason="foreground_transfer_failed")

    assert new_gen == 4
    assert state.generation == 4, "GuiManager-side counter must be bumped"
    # The OLD editor's counter must be bumped to new_gen BEFORE close.
    # _FakeEditor records the value at close-time.
    assert editor.generation_at_close == 4, (
        "OLD editor's _editor_generation must be bumped to new_gen "
        "BEFORE close() runs (round 1 / deepseek finding 8.1)"
    )


def test_rebuild_posts_editor_rebuilt_notification():
    editor = _FakeEditor(_editor_generation=0)
    state = _GuiState(editor=editor, generation=0)
    rebuilder = _build(state)

    rebuilder.rebuild(reason="modern_standby_resume")

    assert len(state.posted) == 1
    posted = state.posted[0]
    assert posted["action"] == EDITOR_REBUILT_ACTION
    # Round-trip via the schema validator to assert shape.
    notification = EditorRebuiltNotification.from_dict(posted)
    assert notification.old_generation == 0
    assert notification.new_generation == 1
    assert notification.reason == "modern_standby_resume"


def test_rebuild_clears_editor_reference_and_destroys_old_editor():
    editor = _FakeEditor(_editor_generation=0)
    state = _GuiState(editor=editor, generation=0)
    rebuilder = _build(state)

    rebuilder.rebuild(reason="r")

    assert state.editor is None, "GuiManager-side reference must be cleared"
    assert editor.close_called is True
    assert editor.delete_later_called is True
    # Section 6's pseudocode runs close() before deleteLater().
    assert editor.method_order == ["close", "deleteLater"]


def test_rebuild_notification_posted_before_destroy():
    """The notification MUST land on the queue BEFORE close/deleteLater.

    Section 6: "Any queued ``insert_editor_word`` / ``retract_editor_text``
    messages that the dispatcher dequeues BEFORE ``_te_window`` is
    recreated will find either (a) ``self._te_window is None``, or (b)
    ``request_generation != new_gen``." For the dequeue-before-recreate
    path to short-circuit cleanly via the editor-None branch, the
    notification must be posted before the editor is destroyed.
    """
    posts_at_close: list[int] = []

    class _RecordingEditor(_FakeEditor):
        def close(inner_self):
            posts_at_close.append(len(state.posted))
            super(_RecordingEditor, inner_self).close()

    editor = _RecordingEditor(_editor_generation=0)
    state = _GuiState(editor=editor, generation=0)
    rebuilder = _build(state)

    rebuilder.rebuild(reason="r")

    assert posts_at_close == [1], (
        "notification must be on the queue before close() fires"
    )


def test_rebuild_returns_new_generation_value():
    state = _GuiState(editor=None, generation=42)
    rebuilder = _build(state)
    assert rebuilder.rebuild(reason="r") == 43


# ---------------------------------------------------------------------------
# No live editor
# ---------------------------------------------------------------------------


def test_rebuild_with_no_live_editor_still_bumps_and_notifies():
    """The orchestrator must run even if no editor is currently live.

    The next ``open_te`` request constructs the fresh editor; the
    rebuild itself only needs to bump the generation and emit the
    notification so Logic-side futures from prior generations get
    failed.
    """
    state = _GuiState(editor=None, generation=5)
    rebuilder = _build(state)

    new_gen = rebuilder.rebuild(reason="r")

    assert new_gen == 6
    assert state.generation == 6
    assert len(state.posted) == 1
    # No editor to set to None, but the setter still ran.
    assert state.editor is None


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------


def test_rebuild_continues_when_notification_post_raises():
    """A queue.put_nowait raise must not abort the rebuild.

    Section 6 pseudocode catches this exception and logs it; the
    Logic-side fan-out is an optimisation, not a correctness fence.
    """
    editor = _FakeEditor(_editor_generation=0)
    state = _GuiState(
        editor=editor,
        generation=0,
        posts_raise=RuntimeError("queue full"),
    )
    rebuilder = _build(state)

    new_gen = rebuilder.rebuild(reason="r")

    assert new_gen == 1
    assert state.generation == 1
    assert state.editor is None
    assert editor.close_called is True
    assert editor.delete_later_called is True


def test_rebuild_continues_when_close_raises():
    editor = _FakeEditor(
        _editor_generation=0,
        close_raises=RuntimeError("Qt object already deleted"),
    )
    state = _GuiState(editor=editor, generation=0)
    rebuilder = _build(state)

    # Must not raise out of the orchestrator.
    rebuilder.rebuild(reason="r")

    # deleteLater still ran even though close raised.
    assert editor.delete_later_called is True
    assert state.generation == 1


def test_rebuild_continues_when_delete_later_raises():
    editor = _FakeEditor(
        _editor_generation=0,
        delete_later_raises=RuntimeError("widget already deleted"),
    )
    state = _GuiState(editor=editor, generation=0)
    rebuilder = _build(state)

    # Must not raise out of the orchestrator.
    rebuilder.rebuild(reason="r")

    assert editor.close_called is True
    assert state.generation == 1


# ---------------------------------------------------------------------------
# RebuildLost helper
# ---------------------------------------------------------------------------


def test_rebuild_lost_from_payload_matches_editor_rebuilt():
    payload = {
        "chars_inserted": 0,
        "chars_requested": -1,
        "chars_removed": 0,
        "replay_chars": 0,
        "failure_reason": "editor_rebuilt",
    }
    exc = RebuildLost.from_payload(
        payload, old_generation=3, new_generation=4, reason="rdp",
    )
    assert isinstance(exc, RebuildLost)
    assert exc.old_generation == 3
    assert exc.new_generation == 4
    assert exc.reason == "rdp"
    assert exc.failure_reason == "editor_rebuilt"


def test_rebuild_lost_from_payload_matches_stale_generation():
    payload = {"chars_inserted": 0, "failure_reason": "stale_generation"}
    exc = RebuildLost.from_payload(payload)
    assert isinstance(exc, RebuildLost)
    assert exc.failure_reason == "stale_generation"


def test_rebuild_lost_from_payload_returns_none_for_other_failures():
    payload = {"chars_inserted": 0, "failure_reason": "no_active_session"}
    assert RebuildLost.from_payload(payload) is None


def test_rebuild_lost_from_payload_returns_none_for_success():
    payload = {"chars_inserted": 5, "failure_reason": ""}
    assert RebuildLost.from_payload(payload) is None


def test_rebuild_lost_from_payload_returns_none_for_non_mapping():
    assert RebuildLost.from_payload(["not", "a", "mapping"]) is None


def test_rebuild_lost_carries_message_with_all_fields():
    exc = RebuildLost(
        old_generation=2,
        new_generation=3,
        reason="rdp",
        failure_reason="editor_rebuilt",
    )
    msg = str(exc)
    assert "old_gen=2" in msg
    assert "new_gen=3" in msg
    assert "rdp" in msg
    assert "editor_rebuilt" in msg


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_default_failure_reason_on_rebuild_lost_is_editor_rebuilt():
    exc = RebuildLost(old_generation=0, new_generation=1, reason="r")
    assert exc.failure_reason == "editor_rebuilt"


def test_old_editor_protocol_attribute_failure_is_logged_not_raised(caplog):
    """A protocol-violating editor (no settable _editor_generation) is
    surfaced via logger.exception but does NOT abort the rebuild.

    In production the editor's attribute is a plain int; this test
    guards against a regression where a future stub or subclass turns
    the attribute into a read-only property.
    """
    class _ReadOnly:
        @property
        def _editor_generation(self):
            return 0

        def close(self):
            pass

        def deleteLater(self):
            pass

    state = _GuiState(editor=_ReadOnly(), generation=0)  # type: ignore[arg-type]
    rebuilder = _build(state)

    with caplog.at_level("ERROR", logger="services.wheelhouse.shared.editor_rebuild"):
        rebuilder.rebuild(reason="r")

    assert state.generation == 1
    # The rebuild completed; a log line is recorded.
    matching = [
        r for r in caplog.records
        if "bumping old editor _editor_generation raised" in r.getMessage()
    ]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Repeated rebuilds
# ---------------------------------------------------------------------------


def test_back_to_back_rebuilds_advance_generation_independently():
    """Two consecutive rebuilds bump the counter twice without corruption.

    The first rebuild sets state.editor to None; the second rebuild
    finds no live editor (matches the "rebuild while editor not yet
    reconstructed" failure mode) but still bumps and emits.
    """
    editor = _FakeEditor(_editor_generation=0)
    state = _GuiState(editor=editor, generation=0)
    rebuilder = _build(state)

    assert rebuilder.rebuild(reason="first") == 1
    assert rebuilder.rebuild(reason="second") == 2
    assert state.generation == 2
    assert len(state.posted) == 2
    # First notification was old=0 new=1; second was old=1 new=2.
    assert state.posted[0]["old_generation"] == 0
    assert state.posted[0]["new_generation"] == 1
    assert state.posted[1]["old_generation"] == 1
    assert state.posted[1]["new_generation"] == 2
