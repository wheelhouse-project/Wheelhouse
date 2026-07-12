"""Tests for EditorIpcResponder (wh-g2-refactor.14).

Covers the GUI-side dispatcher that handles ``insert_editor_word`` and
``retract_editor_text`` requests on the Qt main thread and returns
correlated responses on ``commands_to_logic_queue``. The responder is
a thin glue layer between the raw queue dict and the editor's typed
methods; the tests stub the editor with simple callables so the
contract can be exercised without spinning up Qt.

Coverage:
  * Insert request dispatches to the handler with parsed args and
    enqueues a well-formed response.
  * Retract request dispatches to the handler with parsed args and
    enqueues a well-formed response.
  * Malformed payloads (schema validation failures) do NOT raise out
    of the responder; the responder logs and enqueues no response
    (matches wh-uf54's "graceful degradation for new events" rule).
  * Editor-unavailable path: when the editor accessor returns None,
    the responder enqueues ``stale_generation`` for both insert and
    retract without touching the editor (Section 5 / Section 2
    dispatcher behaviour for ``_te_window is None``).
  * Stale-generation fence: when the request's editor_generation does
    not match the live editor's, the responder enqueues
    ``stale_generation`` without invoking the editor's insert / retract.
  * Handler exceptions are caught and surfaced as
    ``editor_unavailable`` responses so a Qt-side bug does not crash
    the GUI dispatcher.
  * Dispatch runs synchronously (returns when the response has been
    enqueued); the unit test harness uses no threads.
"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Queue

import pytest

from services.wheelhouse.shared.editor_ipc_responder import (
    EditorIpcResponder,
    InsertHandlerResult,
    RetractHandlerResult,
)
from services.wheelhouse.shared.insert_editor_word import (
    ACTION_NAME_REQUEST as INSERT_REQ,
    ACTION_NAME_RESPONSE as INSERT_RESP,
    InsertEditorWordResponse,
)
from services.wheelhouse.shared.insert_editor_word import (
    FAILURE_SUCCESS as INSERT_OK,
    FAILURE_STALE_GENERATION as INSERT_STALE,
    FAILURE_EDITOR_UNAVAILABLE as INSERT_EDITOR_UNAVAIL,
)
from services.wheelhouse.shared.retract_editor_text import (
    ACTION_NAME_REQUEST as RETRACT_REQ,
    ACTION_NAME_RESPONSE as RETRACT_RESP,
    RetractEditorTextResponse,
)
from services.wheelhouse.shared.retract_editor_text import (
    FAILURE_SUCCESS as RETRACT_OK,
    FAILURE_STALE_GENERATION as RETRACT_STALE,
    FAILURE_EDITOR_UNAVAILABLE as RETRACT_EDITOR_UNAVAIL,
)


_RID = "abcd" * 8
_UID = "u" * 16


# ---------------------------------------------------------------------------
# Stub editor
# ---------------------------------------------------------------------------


@dataclass
class _StubEditor:
    """Stand-in for TerminalDictationEditorWindow used by responder tests.

    Has the only fields the responder reads (``_editor_generation``) and
    methods (``insert_word``, ``retract_and_replay``) plus a knob for
    behaviour the tests want to vary.
    """

    generation: int = 0
    insert_calls: list[tuple[str, str]] = None  # type: ignore[assignment]
    retract_calls: list[tuple[int, str, str, bool]] = None  # type: ignore[assignment]
    insert_result: InsertHandlerResult = None  # type: ignore[assignment]
    retract_result: RetractHandlerResult = None  # type: ignore[assignment]
    insert_raises: BaseException | None = None
    retract_raises: BaseException | None = None

    def __post_init__(self):
        if self.insert_calls is None:
            self.insert_calls = []
        if self.retract_calls is None:
            self.retract_calls = []
        if self.insert_result is None:
            self.insert_result = InsertHandlerResult(chars_inserted=0, failure_reason=INSERT_OK)
        if self.retract_result is None:
            self.retract_result = RetractHandlerResult(
                chars_removed=0, replay_chars=0, failure_reason=RETRACT_OK,
            )

    @property
    def _editor_generation(self) -> int:
        return self.generation

    def insert_word(self, text: str, utterance_id: str) -> InsertHandlerResult:
        if self.insert_raises is not None:
            raise self.insert_raises
        self.insert_calls.append((text, utterance_id))
        return self.insert_result

    def retract_and_replay(
        self,
        chars_requested: int,
        utterance_id: str,
        replay_text: str,
        whole_utterance: bool = False,
    ) -> RetractHandlerResult:
        if self.retract_raises is not None:
            raise self.retract_raises
        self.retract_calls.append(
            (chars_requested, utterance_id, replay_text, whole_utterance)
        )
        return self.retract_result


def _drain_queue(q: Queue) -> list[dict]:
    items: list[dict] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Insert dispatch
# ---------------------------------------------------------------------------


def test_insert_dispatch_routes_to_handler_and_enqueues_response():
    editor = _StubEditor(generation=2)
    editor.insert_result = InsertHandlerResult(chars_inserted=5, failure_reason=INSERT_OK)
    q: Queue = Queue()
    responder = EditorIpcResponder(
        get_editor=lambda: editor,
        response_queue=q,
    )
    payload = {
        "action": INSERT_REQ,
        "request_id": _RID,
        "text": "hello",
        "utterance_id": _UID,
        "editor_generation": 2,
    }
    responder.handle(payload)
    assert editor.insert_calls == [("hello", _UID)]
    items = _drain_queue(q)
    assert len(items) == 1
    resp = InsertEditorWordResponse.from_dict(items[0])
    assert resp.request_id == _RID
    assert resp.chars_inserted == 5
    assert resp.failure_reason == INSERT_OK


def test_insert_returns_editor_unavailable_when_editor_is_none():
    """Section 5: 'Treat as stale_generation rather than editor_unavailable
    so Logic's drop-without-retry branch fires.'

    Section 5's dispatcher pseudocode actually returns ``stale_generation``
    on the editor-None branch -- the design doc explicitly chose
    stale_generation over editor_unavailable. Mirror that here.
    """
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: None, response_queue=q)
    payload = {
        "action": INSERT_REQ,
        "request_id": _RID,
        "text": "hello",
        "utterance_id": _UID,
        "editor_generation": 2,
    }
    responder.handle(payload)
    items = _drain_queue(q)
    resp = InsertEditorWordResponse.from_dict(items[0])
    assert resp.failure_reason == INSERT_STALE
    assert resp.chars_inserted == 0


def test_insert_fences_stale_generation():
    editor = _StubEditor(generation=5)
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": INSERT_REQ,
        "request_id": _RID,
        "text": "hello",
        "utterance_id": _UID,
        "editor_generation": 4,  # stale (editor is at 5)
    }
    responder.handle(payload)
    assert editor.insert_calls == []  # editor was NOT touched
    items = _drain_queue(q)
    resp = InsertEditorWordResponse.from_dict(items[0])
    assert resp.failure_reason == INSERT_STALE


def test_insert_catches_handler_exception_as_editor_unavailable():
    editor = _StubEditor(generation=0, insert_raises=RuntimeError("Qt blew up"))
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": INSERT_REQ,
        "request_id": _RID,
        "text": "hello",
        "utterance_id": _UID,
        "editor_generation": 0,
    }
    responder.handle(payload)
    items = _drain_queue(q)
    resp = InsertEditorWordResponse.from_dict(items[0])
    assert resp.failure_reason == INSERT_EDITOR_UNAVAIL


# ---------------------------------------------------------------------------
# Retract dispatch
# ---------------------------------------------------------------------------


def test_retract_dispatch_routes_to_handler_and_enqueues_response():
    editor = _StubEditor(generation=1)
    editor.retract_result = RetractHandlerResult(
        chars_removed=3, replay_chars=4, failure_reason=RETRACT_OK,
    )
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        "chars_requested": 3,
        "utterance_id": _UID,
        "replay_text": "abcd",
        "editor_generation": 1,
    }
    responder.handle(payload)
    assert editor.retract_calls == [(3, _UID, "abcd", False)]
    items = _drain_queue(q)
    resp = RetractEditorTextResponse.from_dict(items[0])
    assert resp.chars_requested == 3
    assert resp.chars_removed == 3
    assert resp.replay_chars == 4
    assert resp.failure_reason == RETRACT_OK


def test_retract_returns_stale_generation_when_editor_is_none():
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: None, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        "chars_requested": 3,
        "utterance_id": _UID,
        "replay_text": "",
        "editor_generation": 0,
    }
    responder.handle(payload)
    items = _drain_queue(q)
    resp = RetractEditorTextResponse.from_dict(items[0])
    assert resp.failure_reason == RETRACT_STALE
    assert resp.chars_removed == 0
    assert resp.replay_chars == 0


def test_retract_fences_stale_generation():
    editor = _StubEditor(generation=3)
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        "chars_requested": 2,
        "utterance_id": _UID,
        "replay_text": "z",
        "editor_generation": 2,  # stale
    }
    responder.handle(payload)
    assert editor.retract_calls == []
    items = _drain_queue(q)
    resp = RetractEditorTextResponse.from_dict(items[0])
    assert resp.failure_reason == RETRACT_STALE


def test_retract_catches_handler_exception_as_editor_unavailable():
    editor = _StubEditor(generation=0, retract_raises=RuntimeError("boom"))
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        "chars_requested": 2,
        "utterance_id": _UID,
        "replay_text": "",
        "editor_generation": 0,
    }
    responder.handle(payload)
    items = _drain_queue(q)
    resp = RetractEditorTextResponse.from_dict(items[0])
    assert resp.failure_reason == RETRACT_EDITOR_UNAVAIL


# ---------------------------------------------------------------------------
# Malformed payload handling
# ---------------------------------------------------------------------------


def test_malformed_insert_payload_drops_silently():
    editor = _StubEditor(generation=0)
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    # Missing 'text' field
    payload = {
        "action": INSERT_REQ,
        "request_id": _RID,
        "utterance_id": _UID,
        "editor_generation": 0,
    }
    # Must not raise.
    responder.handle(payload)
    # The producer awaits with a timeout; no response was enqueued for
    # a malformed request.
    assert _drain_queue(q) == []


def test_malformed_retract_payload_drops_silently():
    editor = _StubEditor(generation=0)
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        # missing chars_requested
        "utterance_id": _UID,
        "replay_text": "",
        "editor_generation": 0,
    }
    responder.handle(payload)
    assert _drain_queue(q) == []


def test_unknown_action_returns_false():
    """handle() returns True when it consumed the message, False when not.

    This lets the GUI's big elif chain delegate to the responder
    without losing fall-through: if the responder did NOT recognise
    the action, the host dispatcher can keep matching other branches.
    """
    editor = _StubEditor(generation=0)
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {"action": "te_show", "request_id": _RID}
    consumed = responder.handle(payload)
    assert consumed is False
    assert _drain_queue(q) == []


def test_known_action_returns_true():
    editor = _StubEditor(generation=0)
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": INSERT_REQ,
        "request_id": _RID,
        "text": "x",
        "utterance_id": _UID,
        "editor_generation": 0,
    }
    assert responder.handle(payload) is True


# ---------------------------------------------------------------------------
# whole_utterance pass-through (wh-editor-retract-ledger-authoritative)
# ---------------------------------------------------------------------------


def test_retract_whole_utterance_passes_flag_and_echoes():
    """whole_utterance=True reaches the editor call and the response
    echoes it, with chars_removed free to exceed the advisory
    chars_requested (mirror drift is the point of the mode)."""
    editor = _StubEditor(generation=1)
    editor.retract_result = RetractHandlerResult(
        chars_removed=11, replay_chars=5, failure_reason=RETRACT_OK,
    )
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        "chars_requested": 0,
        "utterance_id": _UID,
        "replay_text": "hello",
        "editor_generation": 1,
        "whole_utterance": True,
    }
    responder.handle(payload)
    assert editor.retract_calls == [(0, _UID, "hello", True)]
    items = _drain_queue(q)
    resp = RetractEditorTextResponse.from_dict(items[0])
    assert resp.whole_utterance is True
    assert resp.chars_requested == 0
    assert resp.chars_removed == 11
    assert resp.failure_reason == RETRACT_OK


def test_retract_counted_mode_passes_flag_false():
    editor = _StubEditor(generation=1)
    editor.retract_result = RetractHandlerResult(
        chars_removed=3, replay_chars=0, failure_reason=RETRACT_OK,
    )
    q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=q)
    payload = {
        "action": RETRACT_REQ,
        "request_id": _RID,
        "chars_requested": 3,
        "utterance_id": _UID,
        "replay_text": "",
        "editor_generation": 1,
    }
    responder.handle(payload)
    assert editor.retract_calls == [(3, _UID, "", False)]
    items = _drain_queue(q)
    resp = RetractEditorTextResponse.from_dict(items[0])
    assert resp.whole_utterance is False
