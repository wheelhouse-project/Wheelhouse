"""Editor-side per-utterance credit ledger (wh-g2-refactor.15).

Section 3 of ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
is the authoritative reference. This module ships the ledger as a
standalone ``CreditLedger`` class so the persistent editor (slice 6,
wh-g2-refactor.18) can compose it without having to bake the state
machine into ``TerminalDictationEditorWindow``.

The ledger tracks each direct-Qt-insert run with the canonical text
(NFC-normalised, U+2029 / U+2028 mapped back to ``\\n``) and the
UTF-16 code-unit length the editor's ``QTextDocument`` actually
consumed. Section 3's design decisions encoded here:

* ``_canonical_text`` -- the U+2029 / U+2028 mapping and NFC
  normalisation (round 1 / deepseek finding 8.3).
* ``_utf16_len`` -- ``len(s.encode("utf-16-le")) // 2`` (round 2 /
  codex finding 7.3).
* ``_ledger_hash`` -- blake2b digest_size=8 over the canonicalised
  text encoded as UTF-16-LE (round 1 / codex finding B).
* ``_LedgerRun`` -- the per-run dataclass.
* ``RetractResult`` -- the dataclass returned by
  ``retract_and_replay``.
* ``CreditLedger`` -- the state machine.

The names follow the design doc's leading-underscore convention even
though the module is public; the convention marks them as
implementation details of the editor. The naming reflects their
status as private contracts between the ledger and the persistent
editor, not as a stable public API.

The ledger is NOT thread-safe. It is driven from the Qt main thread
in the production wiring; slice 6's editor methods all run on that
thread.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from services.wheelhouse.shared.grapheme import (
    count_grapheme_clusters,
    normalize_line_endings,
    split_at_cluster_boundary_from_right,
)


if TYPE_CHECKING:  # pragma: no cover - import guard for static checkers.
    from PySide6.QtWidgets import QPlainTextEdit


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Failure-reason constants -- mirror Section 2's enumerated values.
# ---------------------------------------------------------------------------


FAILURE_SUCCESS = ""
FAILURE_LEDGER_UNDERRUN = "ledger_underrun"
FAILURE_NO_ACTIVE_SESSION = "no_active_session"
FAILURE_SESSION_MISMATCH = "session_mismatch"
FAILURE_REPLAY_FAILED = "replay_failed"


# ---------------------------------------------------------------------------
# Text-unit helpers.
# ---------------------------------------------------------------------------


# Qt substitutes these characters for newlines on its QTextCursor.selectedText()
# return path; we map them back to ``\n`` before hashing so the digest agrees
# on both sides of a retract.
_PARAGRAPH_SEPARATOR = " "
_LINE_SEPARATOR = " "


def _canonical_text(s: str) -> str:
    """Normalise the text-unit representation for hashing.

    Two transformations run before the digest:

    1. Map Qt's ``QTextCursor.selectedText()`` substitutions back to
       ``\\n``. When a selection spans line breaks, Qt returns U+2029
       (paragraph separator) in place of every ``\\n``, and U+2028
       (line separator) in some edge cases. The hash must see the same
       characters on the insert side (Python string with ``\\n``) and
       the retract side (selectedText() output).
    2. Unicode NFC normalisation. Python may receive NFD from STT
       ("e" + combining acute, U+0065 + U+0301) while Qt's QString
       stores NFC ('e' as U+00E9) on Windows. The blake2b digest over
       UTF-16-LE is byte-sensitive, so an NFD insert and an NFC
       retract would not match. NFC is the canonical form on both
       sides.
    """
    s = s.replace(_PARAGRAPH_SEPARATOR, "\n").replace(_LINE_SEPARATOR, "\n")
    s = normalize_line_endings(s)
    return unicodedata.normalize("NFC", s)


def _utf16_len(s: str) -> int:
    """Return ``s``'s length in UTF-16 code units (Qt cursor positions).

    Qt's internal text encoding is UTF-16, so ``QTextCursor`` positions
    count code units, not code points. For a string containing only
    BMP characters ``_utf16_len(s) == len(s)``. For non-BMP characters
    (emoji, ZWJ-joined sequences, combining marks above U+FFFF) the
    two differ: each non-BMP character contributes 2 UTF-16 code units
    but only 1 Python code point.

    Acceptance criterion for wh-g2-refactor.15: this helper returns
    the count Python sees as ``len(s.encode("utf-16-le")) // 2``.
    """
    return len(s.encode("utf-16-le")) // 2


def _ledger_hash(text: str) -> str:
    """blake2b(8) digest over the canonical UTF-16-LE encoding.

    8-byte digest yields 16 hex characters. blake2b is faster than md5
    on the CPython stdlib and gives 64 bits of collision resistance,
    which is comfortably enough for the lazy-validation use case (the
    digest is compared against a single candidate run, not searched
    over a corpus).

    The caller is responsible for passing the canonicalised text; this
    function does not re-canonicalise. Callers throughout the ledger
    consistently route through ``_canonical_text`` first.
    """
    return hashlib.blake2b(text.encode("utf-16-le"), digest_size=8).hexdigest()


# ---------------------------------------------------------------------------
# Data classes.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LedgerRun:
    """One direct-Qt-insert run.

    Fields:
      * ``start`` -- Qt cursor position (UTF-16 code unit offset in the
        ``QTextDocument``) where this run begins. NOT a Python
        code-point index.
      * ``end`` -- Qt cursor position where this run ends, exclusive.
        Same unit convention as ``start``.
      * ``clusters`` -- grapheme cluster count for this run (the
        retract math walks the document by clusters).
      * ``content_hash`` -- blake2b(8) digest of
        ``_canonical_text(text)`` at insert time. The lazy validator
        at retract time recomputes the digest of
        ``_canonical_text(cursor.selectedText())`` over
        ``[start, end)`` and compares. A mismatch means the user
        edited the document or the run is otherwise stale; the
        validator drops the run.
    """

    start: int
    end: int
    clusters: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class RetractResult:
    """Result of a :meth:`CreditLedger.retract_and_replay` call.

    Fields:
      * ``chars_removed`` -- grapheme-cluster count actually removed.
        On the success path this equals ``chars_requested``.
      * ``replay_chars`` -- UTF-16 code-unit count the replay insert
        wrote. ``0`` on the retract-only success path, ``0`` on every
        failure path.
      * ``failure_reason`` -- ``""`` for success; one of the
        enumerated reasons otherwise (see
        ``services.wheelhouse.shared.retract_editor_text`` for the
        canonical wire-side list).
    """

    chars_removed: int
    replay_chars: int
    failure_reason: str


# ---------------------------------------------------------------------------
# CreditLedger.
# ---------------------------------------------------------------------------


class CreditLedger:
    """Editor-side credit ledger backed by a ``QPlainTextEdit``.

    The ledger expects to own all mutations to its bound ``text_edit``
    during a session. The producer must call :meth:`start_utterance`
    before the first :meth:`insert_word`, and :meth:`retract_and_replay`
    drives the lazy-validation + partial-trim + replay state machine
    described in Section 3 of the G2 design refinements doc.

    The class is decoupled from the persistent editor's lifecycle for
    testability; slice 6 (wh-g2-refactor.18) wires it into
    ``TerminalDictationEditorWindow``.
    """

    def __init__(self, text_edit: "QPlainTextEdit") -> None:
        self._text_edit = text_edit
        self._runs: list[_LedgerRun] = []
        self._utterance_id: str = ""

    # -- accessors ----------------------------------------------------------

    @property
    def runs(self) -> Sequence[_LedgerRun]:
        """A snapshot of the current ledger runs.

        Returned as a tuple so callers can compare against
        ``()`` cheaply for the empty case. Tests rely on iteration
        order matching insertion order.
        """
        return tuple(self._runs)

    @property
    def utterance_id(self) -> str:
        return self._utterance_id

    # -- session lifecycle --------------------------------------------------

    def start_utterance(self, utterance_id: str) -> None:
        """Begin a fresh utterance session.

        Clears the ledger and records the new ``utterance_id``. The
        document itself is NOT cleared; the persistent-editor invariant
        is that prior text remains in the document until ``submit`` or
        ``cancel`` fires (slice 6's responsibility).
        """
        self._runs.clear()
        self._utterance_id = utterance_id

    def end_utterance(self) -> None:
        """Mark the end of an utterance without clearing the ledger.

        Slice 6 calls this on the speech-processor's end-of-utterance
        boundary. The ledger remains valid so a late-arriving retract
        for this utterance can still run; the retract window closes on
        the next ``start_utterance``, ``submit``, ``cancel``, or
        ``hide_editor``.
        """
        # Intentionally no-op for the ledger today; the method exists
        # so slice 6 has a hook for future bookkeeping (telemetry,
        # diagnostic logging) without changing the persistent editor's
        # call shape.

    def submit(self) -> None:
        """Clear the ledger on submit.

        After submit the text has been sent to the terminal; there is
        nothing in the editor to retract. Slice 6 calls this from the
        Enter handler.
        """
        self._runs.clear()
        self._utterance_id = ""

    def cancel(self) -> None:
        """Clear the ledger on cancel.

        Slice 6 calls this from the Esc handler.
        """
        self._runs.clear()
        self._utterance_id = ""

    # -- mutation ----------------------------------------------------------

    def insert_word(self, text: str, utterance_id: str) -> int:
        """Insert ``text`` at the current cursor and append a run.

        Returns the UTF-16 code-unit count written into the document
        (the cursor.position() delta across the insert, equivalent to
        ``_utf16_len(_canonical_text(text))`` since the canonical form
        is what actually gets inserted). This is the value the GUI
        process echoes back to Logic in the
        ``insert_editor_word_response`` ``chars_inserted`` field.

        If ``utterance_id`` does not match the current
        ``self._utterance_id``, the ledger logs a WARNING, resets to
        the new id, and clears prior runs. The defensive fence
        documents Section 3's reset table: the producer should always
        call ``start_utterance`` first, but a missed call must not
        corrupt the ledger silently.

        Empty ``text`` is rejected -- the IPC schema already filters
        empty inserts at the boundary, so routing one through here
        would only ever happen on a producer bug.
        """
        if not text:
            raise ValueError("insert_word requires non-empty text")
        if utterance_id != self._utterance_id:
            logger.warning(
                "ledger utterance_id mismatch: expected %r, got %r; "
                "resetting ledger",
                self._utterance_id,
                utterance_id,
            )
            self._runs.clear()
            self._utterance_id = utterance_id

        cursor = self._text_edit.textCursor()
        start = cursor.position()
        canonical = _canonical_text(text)
        # Insert the canonical form so the document state matches what
        # the ledger hashes and what split_at_cluster_boundary_from_right
        # operates on. Without this, an NFD producer string lands as NFD
        # in the document (some Qt builds) while the ledger stores the
        # NFC cluster count, and the partial-trim split point lands at
        # the wrong UTF-16 position. Also normalises \r\n / \r line
        # endings (done in _canonical_text) so the doc never carries a
        # mix of \r\n and Qt's internal U+2029 paragraph separators.
        cursor.insertText(canonical)
        end = cursor.position()
        # The cursor.position() reads are Qt-native UTF-16 offsets, so
        # no further conversion is needed here. We re-set the cursor
        # so external readers see the post-insert position.
        self._text_edit.setTextCursor(cursor)
        run = _LedgerRun(
            start=start,
            end=end,
            clusters=count_grapheme_clusters(canonical),
            content_hash=_ledger_hash(canonical),
        )
        self._runs.append(run)
        # Return the UTF-16 count Logic sees on the wire.
        return end - start

    # -- retract + replay --------------------------------------------------

    def retract_and_replay(
        self,
        chars_requested: int,
        utterance_id: str,
        replay_text: str,
    ) -> RetractResult:
        """Retract ``chars_requested`` grapheme clusters then optionally
        insert ``replay_text``.

        Both operations run in this call -- no Qt event loop spin
        between them. Returns a non-empty ``failure_reason`` for any
        non-success outcome. The success path requires
        ``chars_removed == chars_requested`` (round 1 / codex finding C,
        Section 3 of the design doc).
        """
        # Importing here keeps the module importable in headless tests
        # that never touch Qt; the editor tests always have Qt loaded
        # via the qapp fixture before they hit retract_and_replay.
        from PySide6.QtGui import QTextCursor

        if utterance_id != self._utterance_id:
            return RetractResult(0, 0, FAILURE_SESSION_MISMATCH)
        if not self._runs:
            return RetractResult(0, 0, FAILURE_NO_ACTIVE_SESSION)

        doc = self._text_edit.document()
        remaining = chars_requested
        peeled_total = 0
        while remaining > 0 and self._runs:
            found, _dropped = self._next_valid_tail_run(doc)
            if found is None:
                break
            run, cursor, actual = found
            # Run validates. Now act on it.
            if run.clusters <= remaining:
                # Remove the entire run.
                cursor.removeSelectedText()
                peeled_total += run.clusters
                remaining -= run.clusters
                self._runs.pop()
                # Shift any earlier runs that begin AT OR AFTER the
                # popped run's end left by the deleted length. For a
                # well-formed append-only ledger this is a no-op; the
                # shift exists as a safety net for future code paths
                # that might insert non-tail runs.
                shift = run.end - run.start
                for r in self._runs:
                    if r.start >= run.end:
                        r.start -= shift
                        r.end -= shift
            else:
                # Partial run removal. Split the canonicalised run
                # text at the grapheme-cluster boundary 'remaining'
                # from the right.
                kept_text, _removed_text = (
                    split_at_cluster_boundary_from_right(actual, remaining)
                )
                # The split point is computed in Qt cursor units
                # (UTF-16 code units), NOT Python code points. A
                # non-BMP grapheme in kept_text is 1 Python code point
                # but 2 UTF-16 code units; len() would land the cursor
                # in the wrong place.
                split_pos = run.start + _utf16_len(kept_text)
                cursor = QTextCursor(doc)
                cursor.setPosition(split_pos)
                cursor.setPosition(run.end, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                peeled_total += remaining
                run.end = split_pos
                run.clusters -= remaining
                run.content_hash = _ledger_hash(_canonical_text(kept_text))
                remaining = 0

        if peeled_total < chars_requested:
            # Underrun -- the ledger emptied or every remaining run
            # failed validation. Logic does NOT replay; the next STT
            # update will heal the document via a normal insertion.
            return RetractResult(peeled_total, 0, FAILURE_LEDGER_UNDERRUN)

        return self._replay_after_retract(peeled_total, replay_text)

    def retract_all_and_replay(
        self,
        utterance_id: str,
        replay_text: str,
    ) -> RetractResult:
        """Retract EVERY run this ledger holds, then optionally insert
        ``replay_text`` (wh-editor-retract-ledger-authoritative).

        The whole-utterance mode for MODE3 retraction: MODE3 always
        retracts the entire utterance's editor content and replays the
        corrected final, so the speech-side cluster mirror is only a
        drift-prone proxy for "all runs" -- an ``insert_editor_word``
        response that times out Logic-side while the GUI insert
        succeeded leaves the mirror below the ledger's true total, and
        a counted retract then under-deletes. This method peels runs
        from the tail with the same lazy hash validation as
        :meth:`retract_and_replay` (an invalidated run is dropped
        uncounted; its text is never deleted blind), with no partial
        split and no underrun concept: peeling everything that
        validates IS success. An empty ledger is success with
        ``chars_removed == 0`` -- when the GUI-side insert genuinely
        failed, the document holds nothing and the replay is exactly
        the heal.
        """
        if utterance_id != self._utterance_id:
            return RetractResult(0, 0, FAILURE_SESSION_MISMATCH)

        doc = self._text_edit.document()
        peeled_total = 0
        dropped_runs = 0
        while True:
            found, dropped = self._next_valid_tail_run(doc)
            dropped_runs += dropped
            if found is None:
                break
            run, cursor, _actual = found
            cursor.removeSelectedText()
            peeled_total += run.clusters
            self._runs.pop()
            # Same non-tail-run safety shift as retract_and_replay.
            shift = run.end - run.start
            for r in self._runs:
                if r.start >= run.end:
                    r.start -= shift
                    r.end -= shift

        if dropped_runs:
            # Reviewer_0 finding .1.1: counted mode surfaces dropped
            # (user-edited / stale) runs as a ledger_underrun that Logic
            # logs and that suppresses the replay; whole mode has no
            # underrun, so without this line a duplicated-text field
            # report would have zero diagnostic trail. The replay still
            # proceeds -- it lands next to the unvalidated residue, and
            # blocking it would drop the corrected final entirely.
            logger.warning(
                "retract_all: dropped %d invalidated run(s) "
                "(document text no longer matches the ledger hash -- "
                "user edit or stale bounds); their text stays in the "
                "document and the replay proceeds next to it",
                dropped_runs,
            )

        return self._replay_after_retract(peeled_total, replay_text)

    def _next_valid_tail_run(self, doc):
        """Drop invalid tail runs; return the first that validates.

        Shared by both retract modes (reviewer_0 finding .1.3 -- the
        hash-validate/peel preamble was hand-duplicated and would have
        diverged on the next validation fix). Returns
        ``((run, cursor, actual_text), dropped)`` where ``cursor`` has
        the run's span selected and ``actual_text`` is the canonical
        selected text, or ``(None, dropped)`` when the ledger ran out.
        ``dropped`` counts the invalid tail runs popped during THIS
        call; callers accumulate it.
        """
        from PySide6.QtGui import QTextCursor

        dropped = 0
        while self._runs:
            run = self._runs[-1]
            doc_chars = doc.characterCount()
            # Qt's characterCount includes the implicit trailing block
            # separator; the run.end was recorded from cursor.position()
            # which is bounded by characterCount() - 1 (you cannot place
            # the cursor past the final paragraph mark). Bounds-check
            # against the inclusive maximum position rather than the
            # exclusive count.
            if run.end >= doc_chars:
                logger.debug(
                    "ledger run end %d >= doc_chars %d; dropping",
                    run.end, doc_chars,
                )
                self._runs.pop()
                dropped += 1
                continue
            cursor = QTextCursor(doc)
            cursor.setPosition(run.start)
            cursor.setPosition(run.end, QTextCursor.KeepAnchor)
            actual = _canonical_text(cursor.selectedText())
            if _ledger_hash(actual) != run.content_hash:
                logger.debug(
                    "ledger run hash mismatch at [%d:%d); dropping",
                    run.start, run.end,
                )
                self._runs.pop()
                dropped += 1
                continue
            return (run, cursor, actual), dropped
        return None, dropped

    def _replay_after_retract(
        self, peeled_total: int, replay_text: str
    ) -> RetractResult:
        """Shared replay step for both retract modes."""
        replay_chars = 0
        if replay_text:
            try:
                cursor = self._text_edit.textCursor()
                replay_start = cursor.position()
                # Insert the canonical form for the same reason
                # insert_word does: keep document state aligned with the
                # canonical state the ledger hashes.
                canonical_replay = _canonical_text(replay_text)
                cursor.insertText(canonical_replay)
                self._text_edit.setTextCursor(cursor)
                replay_end = cursor.position()
                replay_chars = replay_end - replay_start
                self._runs.append(_LedgerRun(
                    start=replay_start,
                    end=replay_end,
                    clusters=count_grapheme_clusters(canonical_replay),
                    content_hash=_ledger_hash(canonical_replay),
                ))
            except Exception as exc:  # pragma: no cover - Qt failure path
                logger.warning(
                    "replay insert failed after successful retract: %s", exc,
                )
                return RetractResult(peeled_total, 0, FAILURE_REPLAY_FAILED)

        return RetractResult(peeled_total, replay_chars, FAILURE_SUCCESS)
