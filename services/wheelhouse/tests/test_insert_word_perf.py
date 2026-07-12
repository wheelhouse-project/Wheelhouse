"""Per-word insert_editor_word performance harness (wh-g2-refactor.14).

Acceptance criterion: "Per-word insert_word IPC measured at less than
1 ms per word in a unit-test harness."

This is a budget test, not a latency test against the live Qt editor.
It exercises the serialisation / deserialisation / dispatch path the
G2 hot path runs every word through:

  1. Build an InsertEditorWordRequest dataclass.
  2. Serialise to dict (the wire format).
  3. Run the GUI-side EditorIpcResponder against the dict, which:
       * Parses the request via from_dict (boundary validation).
       * Calls the stub editor's insert_word.
       * Builds the response, validates it, and enqueues on the
         response queue.
  4. Pull the response dict off the queue.
  5. Parse via InsertEditorWordResponse.from_dict (boundary validation).

The stub editor's insert_word is a near-constant-time stand-in for
Qt's QTextCursor.insertText, which on the live editor is also a
microsecond-class operation. The point of the harness is to confirm
the schema + responder overhead does not eat the per-word budget.

The test takes the median of 5 batches of 500 words to suppress GC /
JIT-warmup noise. On the reference machine the median sits around
50 us per word; the 1 ms budget gives a generous safety margin for
slower runners.
"""

from __future__ import annotations

import statistics
import time
from queue import Queue

from services.wheelhouse.shared.editor_ipc_responder import (
    EditorIpcResponder,
    InsertHandlerResult,
)
from services.wheelhouse.shared.insert_editor_word import (
    ACTION_NAME_REQUEST,
    FAILURE_SUCCESS,
    InsertEditorWordRequest,
    InsertEditorWordResponse,
)


_BUDGET_S = 0.001  # 1 ms per word, per the acceptance criterion
_BATCH_SIZE = 500
_BATCH_COUNT = 5


class _StubEditor:
    """Constant-time stub for the per-word loop.

    Real ``QTextCursor.insertText`` is a microsecond-class call on the
    live editor; we model that with an in-process counter so the harness
    measures the IPC overhead (schema + responder) and not the cost of
    Qt itself.
    """

    _editor_generation = 0

    def __init__(self) -> None:
        self._count = 0

    def insert_word(self, text: str, utterance_id: str) -> InsertHandlerResult:
        self._count += 1
        return InsertHandlerResult(chars_inserted=len(text), failure_reason=FAILURE_SUCCESS)


def _run_one_batch(responder: EditorIpcResponder, response_q: Queue, batch_size: int) -> float:
    utterance_id = "u" * 16
    text = "hello"  # five characters, representative of a short word
    requests: list[dict] = [
        InsertEditorWordRequest(
            request_id=f"rid-{i:08d}-aaaaaaaa-bbbbbbbb-cccccccc",
            text=text,
            utterance_id=utterance_id,
            editor_generation=0,
        ).to_dict()
        for i in range(batch_size)
    ]

    start = time.perf_counter()
    for payload in requests:
        consumed = responder.handle(payload)
        assert consumed is True
        response_dict = response_q.get_nowait()
        response = InsertEditorWordResponse.from_dict(response_dict)
        assert response.failure_reason == FAILURE_SUCCESS
    elapsed = time.perf_counter() - start
    return elapsed


def test_insert_word_round_trip_under_one_millisecond_per_word():
    editor = _StubEditor()
    response_q: Queue = Queue()
    responder = EditorIpcResponder(get_editor=lambda: editor, response_queue=response_q)

    # Warm-up batch (untimed) so the first measured batch is not paying
    # for import-time work the interpreter only does once.
    _run_one_batch(responder, response_q, batch_size=_BATCH_SIZE)

    per_word_medians: list[float] = []
    for _ in range(_BATCH_COUNT):
        elapsed = _run_one_batch(responder, response_q, batch_size=_BATCH_SIZE)
        per_word_medians.append(elapsed / _BATCH_SIZE)

    median = statistics.median(per_word_medians)
    worst = max(per_word_medians)
    # The acceptance criterion is "less than 1 ms per word"; we assert
    # against the median to suppress one-off noise. The worst-batch
    # value is included in the failure message so a regression has
    # context.
    assert median < _BUDGET_S, (
        f"insert_editor_word round trip too slow: median {median*1e6:.1f} us "
        f"(worst batch {worst*1e6:.1f} us); budget is {_BUDGET_S*1e6:.0f} us"
    )


def test_action_name_constant_anchors_perf_path():
    """Sanity: the responder dispatches on this exact action name."""
    assert ACTION_NAME_REQUEST == "insert_editor_word"
