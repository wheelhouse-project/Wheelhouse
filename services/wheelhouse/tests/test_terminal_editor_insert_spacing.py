"""Per-word editor insert applies TextPerfector spacing (wh-editor-retract-dup).

The persistent dictation editor's class docstring states that text perfection
(spacing, capitalization) is applied locally, "since it needs cursor position
context". But ``insert_word`` handed the raw word straight to the CreditLedger
with no perfection, so consecutive words concatenated with no separating space
(``but`` + ``lets`` -> ``butlets``).

This stayed invisible in production because the routing bug meant only the
FIRST word of an utterance ever reached ``insert_word``; words 2..N took the
legacy path, which added their own spacing. Now that editor routing is sticky
(every word of an editor utterance reaches the ledger), ``insert_word`` must
apply the same TextPerfector pass the legacy path used, or multi-word editor
dictation loses its inter-word spaces.

These tests use pytest-qt's auto-provided ``qapp`` fixture (a manual one
conflicts -- see test_terminal_editor_window.py).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def editor_window(qapp):
    from terminal_editor_window import TerminalDictationEditorWindow

    window = TerminalDictationEditorWindow()
    yield window
    window.hide_editor()


def test_consecutive_inserts_are_space_separated(editor_window):
    """Three words inserted one at a time are spaced, not concatenated.

    Casing is preserved verbatim (no sentence-start capitalization) because
    the editor passes capitalize=False -- its contents go to a shell where
    case is significant (wh-editor-retract-dup.1.2).
    """
    editor_window.insert_word("but", "66")
    editor_window.insert_word("lets", "66")
    editor_window.insert_word("talk", "66")

    assert editor_window._text_edit.toPlainText() == "but lets talk"


def test_insert_word_reports_cluster_count_including_leading_space(editor_window):
    """clusters_inserted includes the leading space TextPerfector adds.

    Word 1 ("but") reports 3; word 2 (" lets") reports 5. The speech side
    accumulates clusters_inserted (not chars_inserted) so a later retract --
    which peels grapheme clusters -- spans the whole inserted run including
    the spaces (wh-editor-retract-dup.1.1).
    """
    first = editor_window.insert_word("but", "66")
    second = editor_window.insert_word("lets", "66")

    assert first.clusters_inserted == 3
    assert second.clusters_inserted == 5
    # For BMP/ASCII the UTF-16 delta coincides with the cluster count.
    assert first.chars_inserted == 3
    assert second.chars_inserted == 5


def test_astral_word_reports_clusters_not_utf16(editor_window):
    """An astral-plane char counts as 1 cluster but 2 UTF-16 code units.

    This is the case the unit fix exists for: accumulating chars_inserted
    (UTF-16) would over-request at retract and underrun. clusters_inserted
    is the retract-safe count.
    """
    result = editor_window.insert_word("\U0001F680", "66")  # rocket emoji

    assert result.clusters_inserted == 1
    assert result.chars_inserted == 2


def test_lowercase_command_word_not_capitalized(editor_window):
    """A shell command dictated first stays lowercase (no Git status)."""
    editor_window.insert_word("git", "66")
    editor_window.insert_word("status", "66")

    assert editor_window._text_edit.toPlainText() == "git status"


def test_punctuation_word_attaches_without_space(editor_window):
    """Punctuation does not get a leading space (TextPerfector rule)."""
    editor_window.insert_word("hello", "66")
    editor_window.insert_word(",", "66")
    editor_window.insert_word("world", "66")

    assert editor_window._text_edit.toPlainText() == "hello, world"

def test_retract_and_replay_whole_utterance_peels_all_runs(editor_window):
    """wh-editor-retract-ledger-authoritative: whole_utterance=True routes
    to the ledger's retract-all path, removing every run regardless of any
    requested count (0 here -- the fully-drifted-mirror case)."""
    editor_window.insert_word("but", "66")
    editor_window.insert_word("lets", "66")
    result = editor_window.retract_and_replay(
        0, utterance_id="66", replay_text="but let's",
        whole_utterance=True,
    )
    assert result.failure_reason == ""
    assert result.chars_removed == 8  # "but" + " lets"
    assert editor_window._text_edit.toPlainText() == "but let's"


def test_retract_and_replay_counted_mode_unchanged(editor_window):
    editor_window.insert_word("but", "66")
    editor_window.insert_word("lets", "66")
    result = editor_window.retract_and_replay(
        5, utterance_id="66", replay_text="",
    )
    assert result.failure_reason == ""
    assert result.chars_removed == 5
    assert editor_window._text_edit.toPlainText() == "but"
