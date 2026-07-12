"""Tests for the editor-side credit ledger (wh-g2-refactor.15).

Section 3 of ``docs/design/2026-05-20-g2-refactor-design-refinements.md``
specifies an editor-side per-utterance credit ledger that tracks each
direct-Qt-insert run with the canonical text (NFC-normalised) and the
UTF-16 code-unit length the editor's ``QTextDocument`` actually
consumed. The ledger lives in
``services.wheelhouse.shared.ledger`` as a standalone ``CreditLedger``
class; slice 6 (wh-g2-refactor.18) wires it into the persistent
editor's lifecycle. Slice 3 ships the ledger as a pure data structure
backed by a ``QPlainTextEdit`` document the test instantiates directly.

The module exports:

* ``_canonical_text(s)`` -- the NFC + U+2029/U+2028 normaliser.
* ``_utf16_len(s)`` -- ``len(s.encode("utf-16-le")) // 2``.
* ``_ledger_hash(text)`` -- blake2b(8) over ``text.encode("utf-16-le")``.
* ``_LedgerRun`` -- the per-run dataclass.
* ``RetractResult`` -- the result dataclass returned by
  ``retract_and_replay``.
* ``CreditLedger`` -- the state machine.

The names follow the design doc's leading-underscore convention even
though the module is public; the convention marks them as
implementation details of the editor.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# _utf16_len -- pure helper, no Qt dependency.
# ---------------------------------------------------------------------------


def test_utf16_len_empty_string_returns_zero():
    from services.wheelhouse.shared.ledger import _utf16_len

    assert _utf16_len("") == 0


def test_utf16_len_ascii_matches_python_len():
    from services.wheelhouse.shared.ledger import _utf16_len

    assert _utf16_len("hello") == 5


def test_utf16_len_matches_encode_helper_for_ascii():
    from services.wheelhouse.shared.ledger import _utf16_len

    s = "hello world"
    assert _utf16_len(s) == len(s.encode("utf-16-le")) // 2


def test_utf16_len_non_bmp_emoji_is_two_units_not_one():
    """A non-BMP code point is 1 Python code point but 2 UTF-16 code
    units."""
    from services.wheelhouse.shared.ledger import _utf16_len

    rocket = "\U0001F680"
    assert len(rocket) == 1
    assert _utf16_len(rocket) == 2
    assert _utf16_len(rocket) == len(rocket.encode("utf-16-le")) // 2


def test_utf16_len_matches_encode_helper_across_regression_inputs():
    """Acceptance criterion for the slice -- _utf16_len returns the
    count Python sees as ``len(s.encode("utf-16-le")) // 2`` for every
    regression input the slice ships tests against."""
    from services.wheelhouse.shared.ledger import _utf16_len

    regression_inputs = [
        "",
        "a",
        "hello",
        "hello, world!",
        "\U0001F680",                              # single emoji
        "rocket \U0001F680 ship",                  # ASCII around emoji
        "café",                                     # combining mark (NFC form)
        "café",                               # combining mark (NFD form)
        "\U0001F468‍\U0001F469‍\U0001F467",  # ZWJ family
        "\U0001F44D\U0001F3FD",                    # thumbs up + skin tone
        "\U0001F1FA\U0001F1F8",                    # US flag (two RIs)
        "line1\nline2",                            # newline
        "line1 line2",                        # line separator
        "line1 line2",                        # paragraph separator
        "αβγδε",                                   # mixed-script Greek
        "日本語",                                   # CJK BMP
        "\U00020000",                              # CJK extension B (non-BMP)
        "rocket \U0001F680‍\U0001F4A8 trail", # ZWJ + non-BMP
    ]
    for s in regression_inputs:
        assert _utf16_len(s) == len(s.encode("utf-16-le")) // 2, s


# ---------------------------------------------------------------------------
# _canonical_text -- NFC + U+2029/U+2028 normalisation.
# ---------------------------------------------------------------------------


def test_canonical_text_passes_through_ascii_unchanged():
    from services.wheelhouse.shared.ledger import _canonical_text

    assert _canonical_text("hello") == "hello"


def test_canonical_text_paragraph_separator_normalization():
    """Round 1 / deepseek finding 8.3. ``_canonical_text`` maps the
    paragraph separator (U+2029) that Qt's ``QTextCursor.selectedText``
    substitutes for ``\\n`` back to ``\\n``."""
    from services.wheelhouse.shared.ledger import _canonical_text

    assert _canonical_text("line1 line2") == "line1\nline2"


def test_canonical_text_line_separator_normalization():
    from services.wheelhouse.shared.ledger import _canonical_text

    assert _canonical_text("line1 line2") == "line1\nline2"


def test_canonical_text_nfd_input_returns_nfc():
    """Python may receive NFD from STT ('e' + combining acute); Qt's
    QString stores NFC ('e' as U+00E9) on Windows. The canonicaliser
    folds both into NFC so the blake2b digest agrees on both sides."""
    from services.wheelhouse.shared.ledger import _canonical_text

    nfd = "café"
    nfc = "café"
    assert _canonical_text(nfd) == nfc
    # NFC input must round-trip unchanged.
    assert _canonical_text(nfc) == nfc


def test_canonical_text_zwj_emoji_family_round_trips():
    """ZWJ-joined family glyph must survive _canonical_text intact so
    the insert-time and retract-time hashes agree."""
    from services.wheelhouse.shared.ledger import _canonical_text

    family = (
        "\U0001F468‍\U0001F469‍\U0001F467"
    )
    assert _canonical_text(family) == family


def test_canonical_text_mixed_newline_and_combining_mark():
    """A multi-line string with an NFD combining mark on one line is
    canonicalised in one pass."""
    from services.wheelhouse.shared.ledger import _canonical_text

    raw = "café line2"
    assert _canonical_text(raw) == "café\nline2"


# ---------------------------------------------------------------------------
# _ledger_hash -- canonical input + blake2b(8) digest.
# ---------------------------------------------------------------------------


def test_ledger_hash_is_deterministic():
    from services.wheelhouse.shared.ledger import _ledger_hash

    assert _ledger_hash("hello") == _ledger_hash("hello")


def test_ledger_hash_is_16_hex_chars():
    """blake2b digest_size=8 means 8 bytes = 16 hex characters."""
    from services.wheelhouse.shared.ledger import _ledger_hash

    digest = _ledger_hash("hello")
    assert isinstance(digest, str)
    assert len(digest) == 16
    int(digest, 16)  # raises if not valid hex


def test_ledger_hash_distinct_inputs_produce_distinct_digests():
    from services.wheelhouse.shared.ledger import _ledger_hash

    assert _ledger_hash("hello") != _ledger_hash("world")


# ---------------------------------------------------------------------------
# CreditLedger -- state machine, exercised through a real QPlainTextEdit
# document. The tests use the session-scoped ``qapp`` fixture from
# conftest (pytest-qt provides ``qtbot``/``qapp`` automatically when
# installed).
# ---------------------------------------------------------------------------


@pytest.fixture
def text_edit(qapp):
    """A fresh ``QPlainTextEdit`` per test, parented to nothing."""
    from PySide6.QtWidgets import QPlainTextEdit

    edit = QPlainTextEdit()
    yield edit
    edit.deleteLater()


@pytest.fixture
def ledger(text_edit):
    """A fresh ``CreditLedger`` bound to ``text_edit``."""
    from services.wheelhouse.shared.ledger import CreditLedger

    return CreditLedger(text_edit)


def _insert_word(ledger, text, utterance_id="utt-1"):
    """Drive an insert through the ledger and return chars_inserted."""
    return ledger.insert_word(text, utterance_id)


# --- start_utterance / append basics ---------------------------------------


def test_new_ledger_starts_empty(ledger):
    assert ledger.runs == ()
    assert ledger.utterance_id == ""


def test_start_utterance_clears_existing_runs(ledger):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    assert len(ledger.runs) == 1
    ledger.start_utterance("utt-2")
    assert ledger.runs == ()
    assert ledger.utterance_id == "utt-2"


def test_insert_word_appends_a_run(ledger):
    ledger.start_utterance("utt-1")
    chars = _insert_word(ledger, "hello")
    assert chars == 5
    assert len(ledger.runs) == 1
    run = ledger.runs[0]
    assert run.start == 0
    assert run.end == 5
    assert run.clusters == 5


def test_insert_word_records_utf16_end_for_non_bmp(ledger):
    """Acceptance: chars_inserted is the UTF-16 code-unit count."""
    ledger.start_utterance("utt-1")
    chars = _insert_word(ledger, "\U0001F680")  # single rocket
    assert chars == 2
    run = ledger.runs[0]
    assert run.start == 0
    assert run.end == 2
    assert run.clusters == 1


def test_insert_word_mismatched_utterance_resets_ledger(ledger):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "old")
    # Mismatched utterance id is the documented defensive fence.
    _insert_word(ledger, "new", utterance_id="utt-2")
    assert ledger.utterance_id == "utt-2"
    assert len(ledger.runs) == 1
    assert ledger.runs[0].clusters == 3


# --- retract_and_replay: success paths -------------------------------------


def test_retract_full_run_removes_text_and_pops_run(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    result = ledger.retract_and_replay(5, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 5
    assert result.replay_chars == 0
    assert ledger.runs == ()
    assert text_edit.toPlainText() == ""


def test_retract_partial_run_keeps_remainder(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    result = ledger.retract_and_replay(2, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 2
    assert text_edit.toPlainText() == "hel"
    assert len(ledger.runs) == 1
    remaining = ledger.runs[0]
    assert remaining.start == 0
    assert remaining.end == 3
    assert remaining.clusters == 3


def test_retract_and_replay_inserts_replay_text(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "helo")  # typo
    result = ledger.retract_and_replay(4, "utt-1", "hello")
    assert result.failure_reason == ""
    assert result.chars_removed == 4
    assert result.replay_chars == 5
    assert text_edit.toPlainText() == "hello"
    assert len(ledger.runs) == 1
    # The replay creates a fresh run for a follow-up retract.
    assert ledger.runs[0].clusters == 5


def test_retract_spans_two_runs(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "abc")
    _insert_word(ledger, "def")
    result = ledger.retract_and_replay(4, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 4
    assert text_edit.toPlainText() == "ab"
    assert len(ledger.runs) == 1
    assert ledger.runs[0].clusters == 2


# --- retract_and_replay: failure paths -------------------------------------


def test_retract_session_mismatch_returns_failure(ledger):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    result = ledger.retract_and_replay(1, "utt-different", "")
    assert result.failure_reason == "session_mismatch"
    assert result.chars_removed == 0
    assert result.replay_chars == 0


def test_retract_no_active_session_returns_failure(ledger):
    """Empty ledger on the matching utterance produces no_active_session.

    The gate order (session mismatch first, then empty ledger) is from
    Section 3 of the design doc -- matching utterance with an empty
    ledger is the documented ``no_active_session`` case.
    """
    ledger.start_utterance("utt-1")
    # start_utterance does not append a run; the ledger is empty but
    # the utterance id matches.
    result = ledger.retract_and_replay(1, "utt-1", "")
    assert result.failure_reason == "no_active_session"


def test_retract_underrun_returns_ledger_underrun(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hi")
    result = ledger.retract_and_replay(5, "utt-1", "replacement")
    assert result.failure_reason == "ledger_underrun"
    # The ledger removes what it can.
    assert result.chars_removed == 2
    # Replay is NOT performed on the underrun path.
    assert result.replay_chars == 0
    # The document is now empty -- the ledger exhausted its runs.
    assert text_edit.toPlainText() == ""


# --- named regression tests from the design doc ----------------------------


def test_emoji_plus_combining_mark_round_trips(ledger, text_edit):
    """Insert ASCII + an emoji + a combining-mark base, then retract and
    replay. No index drift, document ends in the replay text."""
    ledger.start_utterance("utt-1")
    original = "hi \U0001F680 café"
    _insert_word(ledger, original)
    # The cluster count is 3 (h, i, ' ') + 1 (rocket) + 1 (' ') + 4
    # (c, a, f, é) = 9.
    total_clusters = ledger.runs[0].clusters
    assert total_clusters == 9
    result = ledger.retract_and_replay(total_clusters, "utt-1", "replaced")
    assert result.failure_reason == ""
    assert result.chars_removed == total_clusters
    assert text_edit.toPlainText() == "replaced"


def test_surrogate_pair_retraction_is_exact(ledger, text_edit):
    """A non-BMP code point occupies 2 UTF-16 code units. Retracting
    that one cluster removes exactly those 2 code units from the
    document, not 1."""
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "x\U0001F680y")  # x, rocket, y -- 3 clusters
    result = ledger.retract_and_replay(1, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 1
    # After removing one cluster from the right, "y" is gone, the
    # rocket stays. Document has 3 UTF-16 code units left (x + rocket).
    assert text_edit.toPlainText() == "x\U0001F680"


def test_mixed_script_run_round_trips(ledger, text_edit):
    """A run containing Latin, Greek, CJK BMP and CJK non-BMP characters
    retracts to empty without index drift."""
    ledger.start_utterance("utt-1")
    text = "ab αβ 日本 \U00020000"
    _insert_word(ledger, text)
    total = ledger.runs[0].clusters
    result = ledger.retract_and_replay(total, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == total
    assert text_edit.toPlainText() == ""


def test_retraction_across_multi_code_unit_grapheme(ledger, text_edit):
    """Retract one cluster across a grapheme that spans multiple UTF-16
    code units. The cluster is the rocket emoji (2 UTF-16 code units,
    1 Python code point). Retracting one cluster from the right of
    ``"a\U0001F680"`` must leave exactly ``"a"`` in the document,
    not ``"a\uD83D"`` (high surrogate orphaned)."""
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "a\U0001F680")
    result = ledger.retract_and_replay(1, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 1
    assert text_edit.toPlainText() == "a"


def test_retraction_across_zwj_family_grapheme(ledger, text_edit):
    """Retract one cluster across a ZWJ-joined family emoji (7 Python
    code points, 11 UTF-16 code units). The family is ONE cluster, so
    retracting one cluster from ``"hi <family>"`` must remove the entire
    family in one pass."""
    ledger.start_utterance("utt-1")
    family = (
        "\U0001F468‍\U0001F469‍\U0001F467"
    )
    _insert_word(ledger, "hi " + family)
    # 3 ASCII clusters + 1 family cluster = 4 clusters.
    assert ledger.runs[0].clusters == 4
    result = ledger.retract_and_replay(1, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 1
    assert text_edit.toPlainText() == "hi "


# --- successive retract_and_replay calls on the same utterance -------------


def test_replay_text_is_retractable_next_round(ledger, text_edit):
    """After retract_and_replay inserts the replay text, a follow-up
    retract on the same utterance must validate and peel the replayed
    run."""
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "helo")
    result1 = ledger.retract_and_replay(4, "utt-1", "hello")
    assert result1.failure_reason == ""
    assert text_edit.toPlainText() == "hello"
    result2 = ledger.retract_and_replay(5, "utt-1", "")
    assert result2.failure_reason == ""
    assert result2.chars_removed == 5
    assert text_edit.toPlainText() == ""


# --- finding wh-g2-refactor.25.1: insert canonicalises into the document ---


def test_canonical_text_normalises_crlf_to_lf():
    """``\\r\\n`` and bare ``\\r`` must canonicalise to ``\\n`` so the
    insert-side hash matches the retract-side hash (Qt's
    ``QTextCursor.insertText`` converts ``\\r\\n`` to its internal
    paragraph separator, which ``selectedText`` returns as U+2029, which
    in turn maps to ``\\n``). Without this, ``\\r\\n`` text silently
    fails the hash gate on retract (finding wh-g2-refactor.25.1 case B).
    """
    from services.wheelhouse.shared.ledger import _canonical_text

    assert _canonical_text("line1\r\nline2") == "line1\nline2"
    assert _canonical_text("line1\rline2") == "line1\nline2"


def test_insert_word_writes_canonical_into_document(ledger, text_edit):
    """An NFD producer string must land as NFC in the document so the
    partial-trim split point computed in canonical UTF-16 units lands
    at the correct cursor position. Finding wh-g2-refactor.25.1 case A.
    """
    ledger.start_utterance("utt-1")
    nfd = "café world"  # e + combining acute, NFD
    nfc = "café world"  # é precomposed, NFC
    _insert_word(ledger, nfd)
    # After the canonical-insert fix, the document holds NFC text.
    assert text_edit.toPlainText() == nfc
    # Now retract 4 clusters from the right ("orld"). With raw insertion
    # this would mis-position the split because the NFC kept-text length
    # diverges from the document's NFD layout; with canonical insertion
    # the document is NFC and the split lands correctly.
    result = ledger.retract_and_replay(4, "utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 4
    assert text_edit.toPlainText() == nfc[:-4]


def test_insert_then_retract_with_crlf_input_round_trips(ledger, text_edit):
    """A producer string containing ``\\r\\n`` must round-trip through
    insert and retract without the run silently failing the hash gate.
    Finding wh-g2-refactor.25.1 case B."""
    ledger.start_utterance("utt-1")
    raw = "line1\r\nline2"
    _insert_word(ledger, raw)
    total_clusters = ledger.runs[0].clusters
    result = ledger.retract_and_replay(total_clusters, "utt-1", "")
    # Without the \r\n canonicalisation, retract would silently drop the
    # run via FAILURE_LEDGER_UNDERRUN and the document would still hold
    # text. With the fix, every cluster comes off and the document is
    # empty.
    assert result.failure_reason == ""
    assert result.chars_removed == total_clusters
    assert text_edit.toPlainText() == ""


# --- retract_all_and_replay (wh-editor-retract-ledger-authoritative) --------
#
# MODE3 always retracts the entire utterance's editor content, so the
# speech-side cluster mirror is only a drift-prone proxy for "all runs".
# retract_all_and_replay peels every run the ledger holds regardless of
# any requested count, eliminating the mirror as a correctness dependency.


def test_retract_all_removes_every_run_regardless_of_mirror_drift(
    ledger, text_edit
):
    """The bead's core case: the speech-side mirror under-counted (an
    insert response timed out Logic-side but the word landed), so a
    counted retract would under-delete. retract_all peels everything."""
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    _insert_word(ledger, " world")
    result = ledger.retract_all_and_replay("utt-1", "")
    assert result.failure_reason == ""
    assert result.chars_removed == 11
    assert result.replay_chars == 0
    assert ledger.runs == ()
    assert text_edit.toPlainText() == ""


def test_retract_all_with_replay_inserts_and_ledgers_replay(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "helo")
    _insert_word(ledger, " wrld")
    result = ledger.retract_all_and_replay("utt-1", "hello world")
    assert result.failure_reason == ""
    assert result.chars_removed == 9
    assert result.replay_chars == 11
    assert text_edit.toPlainText() == "hello world"
    assert len(ledger.runs) == 1
    assert ledger.runs[0].clusters == 11


def test_retract_all_session_mismatch_removes_nothing(ledger, text_edit):
    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    result = ledger.retract_all_and_replay("utt-OTHER", "replacement")
    assert result.failure_reason == "session_mismatch"
    assert result.chars_removed == 0
    assert result.replay_chars == 0
    assert text_edit.toPlainText() == "hello"


def test_retract_all_empty_ledger_succeeds_and_still_replays(
    ledger, text_edit
):
    """An empty ledger is NOT an error for the whole-utterance mode: if
    the GUI-side insert genuinely failed, the document holds nothing and
    the replay is exactly the heal the retract exists to deliver."""
    ledger.start_utterance("utt-1")
    result = ledger.retract_all_and_replay("utt-1", "hello")
    assert result.failure_reason == ""
    assert result.chars_removed == 0
    assert result.replay_chars == 5
    assert text_edit.toPlainText() == "hello"
    assert len(ledger.runs) == 1


def test_retract_all_drops_invalid_tail_run_but_peels_valid_rest(
    ledger, text_edit
):
    """A run whose document text no longer matches its hash (user edit)
    is dropped uncounted; the remaining valid runs still peel."""
    from PySide6.QtGui import QTextCursor

    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    _insert_word(ledger, " world")
    # Corrupt the tail run's document text without ledger knowledge.
    cursor = QTextCursor(text_edit.document())
    cursor.setPosition(5)
    cursor.setPosition(11, QTextCursor.KeepAnchor)
    cursor.insertText(" WURLD")
    result = ledger.retract_all_and_replay("utt-1", "")
    assert result.failure_reason == ""
    # Only the valid first run's clusters count.
    assert result.chars_removed == 5
    assert ledger.runs == ()
    # The corrupted span stays (the ledger never deletes unvalidated text).
    assert text_edit.toPlainText() == " WURLD"


def test_retract_all_warns_when_invalid_runs_dropped(ledger, text_edit, caplog):
    """Reviewer_0 finding .1.1: whole mode has no underrun, so a dropped
    (user-edited) run must leave a WARNING diagnostic trail -- counted
    mode surfaces the same situation as ledger_underrun."""
    import logging as _logging
    from PySide6.QtGui import QTextCursor

    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    _insert_word(ledger, " world")
    cursor = QTextCursor(text_edit.document())
    cursor.setPosition(5)
    cursor.setPosition(11, QTextCursor.KeepAnchor)
    cursor.insertText(" WURLD")
    with caplog.at_level(_logging.WARNING, logger="services.wheelhouse.shared.ledger"):
        result = ledger.retract_all_and_replay("utt-1", "")
    assert result.failure_reason == ""
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert any("dropped 1 invalidated run" in r.getMessage() for r in warnings)


def test_retract_all_clean_path_logs_no_warning(ledger, text_edit, caplog):
    import logging as _logging

    ledger.start_utterance("utt-1")
    _insert_word(ledger, "hello")
    with caplog.at_level(_logging.WARNING, logger="services.wheelhouse.shared.ledger"):
        result = ledger.retract_all_and_replay("utt-1", "")
    assert result.failure_reason == ""
    assert [r for r in caplog.records if r.levelno >= _logging.WARNING] == []
