"""Tests for ui/uia_phrase_select.py -- find and select a spoken phrase.

wh-spoken-phrase-select.2. The module searches the focused text control
for the words the user spoke and selects the first match. Every call
happens inside the target application through UI Automation, so no
document text crosses a process boundary.

The six cases the bead names: the phrase is found, the phrase is absent,
there is no focused control, the control has no text pattern, the library
raises, and focus changed between the capture and the select.

Every test passes focus_reader, so no test makes a real UI Automation
call for the focus check.
"""
import logging
from unittest.mock import MagicMock

import pytest

from ui.uia_phrase_select import (
    PHRASE_FAILED,
    PHRASE_FOCUS_CHANGED,
    PHRASE_FOCUS_UNKNOWN,
    PHRASE_NOT_FOUND,
    PHRASE_NO_CONTROL,
    PHRASE_NO_TEXT_PATTERN,
    PHRASE_SELECTED,
    select_phrase_in_control,
)


def _control_that_finds(matched_text="brown fox", runtime_id=(42, 1, 2)):
    """A control whose search finds the phrase and whose select succeeds.

    ``runtime_id`` names which control this wrapper stands for. Two
    wrappers with the same runtime id are two reads of one control.
    """
    found_range = MagicMock()
    found_range.GetText.return_value = matched_text
    found_range.Select.return_value = True

    document_range = MagicMock()
    document_range.FindText.return_value = found_range

    text_pattern = MagicMock()
    text_pattern.DocumentRange = document_range

    control = MagicMock()
    control.GetPattern.return_value = text_pattern
    control.GetRuntimeId.return_value = list(runtime_id)
    control._found_range = found_range
    control._document_range = document_range
    return control


def _select(control, phrase, focus=None):
    """Run the search with a focus reader that keeps focus where it was."""
    reader = (lambda: control) if focus is None else focus
    return select_phrase_in_control(control, phrase, focus_reader=reader)


class TestPhraseFound:
    """The ordinary case: the words are in the document."""

    def test_reports_selected(self):
        control = _control_that_finds()
        outcome, matched = _select(control, "brown fox")
        assert outcome == PHRASE_SELECTED
        assert matched == "brown fox"

    def test_searches_forward_and_ignores_letter_case(self):
        control = _control_that_finds()
        _select(control, "Brown Fox")
        control._document_range.FindText.assert_called_once_with(
            "Brown Fox", False, True
        )

    def test_select_does_not_sleep(self):
        """The library sleeps 0.5 s by default. The probe measured the
        select at 2.18 ms in Word and 4.31 ms in Notepad without it, so
        the default wait is pure delay on a voice command."""
        control = _control_that_finds()
        _select(control, "brown fox")
        control._found_range.Select.assert_called_once_with(waitTime=0)

    def test_asks_for_the_text_pattern(self):
        control = _control_that_finds()
        _select(control, "brown fox")
        assert control.GetPattern.called

    def test_a_select_that_returns_false_is_still_a_failure(self):
        control = _control_that_finds()
        control._found_range.Select.return_value = False
        outcome, _ = _select(control, "brown fox")
        assert outcome == PHRASE_FAILED


class TestPhraseAbsent:
    """The search ran and matched nothing."""

    def test_reports_not_found(self):
        control = _control_that_finds()
        control._document_range.FindText.return_value = None
        outcome, matched = _select(control, "purple cow")
        assert outcome == PHRASE_NOT_FOUND
        assert matched is None

    def test_selects_nothing(self):
        control = _control_that_finds()
        control._document_range.FindText.return_value = None
        _select(control, "purple cow")
        assert not control._found_range.Select.called


class TestNoControl:
    """Nothing holds the keyboard focus."""

    def test_reports_no_control(self):
        outcome, matched = _select(None, "brown fox")
        assert outcome == PHRASE_NO_CONTROL
        assert matched is None


class TestNoTextPattern:
    """The focused control is not a text control."""

    def test_reports_no_text_pattern(self):
        control = MagicMock()
        control.GetPattern.return_value = None
        outcome, matched = _select(control, "brown fox")
        assert outcome == PHRASE_NO_TEXT_PATTERN
        assert matched is None


class TestLibraryRaises:
    """Any UI Automation call can raise when the application is busy."""

    def test_get_pattern_raises(self):
        control = MagicMock()
        control.GetPattern.side_effect = OSError("the application is busy")
        outcome, matched = _select(control, "brown fox")
        assert outcome == PHRASE_FAILED
        assert matched is None

    def test_find_text_raises(self):
        control = _control_that_finds()
        control._document_range.FindText.side_effect = OSError("busy")
        outcome, _ = _select(control, "brown fox")
        assert outcome == PHRASE_FAILED

    def test_select_raises(self):
        control = _control_that_finds()
        control._found_range.Select.side_effect = OSError("busy")
        outcome, _ = _select(control, "brown fox")
        assert outcome == PHRASE_FAILED

    def test_document_range_raises(self):
        control = MagicMock()
        text_pattern = MagicMock()
        type(text_pattern).DocumentRange = property(
            lambda _self: (_ for _ in ()).throw(OSError("busy"))
        )
        control.GetPattern.return_value = text_pattern
        outcome, _ = _select(control, "brown fox")
        assert outcome == PHRASE_FAILED

    def test_a_matched_text_read_that_raises_still_reports_selected(self):
        """GetText only names the match for the log line. A failure to
        read it must not turn a good selection into a failure."""
        control = _control_that_finds()
        control._found_range.GetText.side_effect = OSError("busy")
        outcome, matched = _select(control, "brown fox")
        assert outcome == PHRASE_SELECTED
        assert matched is None

    def test_does_not_log_the_phrase(self, caplog):
        """The words the user spoke are theirs. A failure line must not
        carry the phrase into the log file."""
        control = _control_that_finds()
        control._document_range.FindText.side_effect = OSError("busy")
        with caplog.at_level(logging.DEBUG):
            _select(control, "my bank password is hunter2")
        assert "hunter2" not in caplog.text


class TestFocusChanged:
    """The user moved to another window between the capture and the select.

    The probe measured on 2026-08-19 that focus stayed put in every run,
    even with a five second delay. This case still needs an answer,
    because a person can click during that time.

    The comparison uses the runtime id, not the Python object.
    uiautomation builds a new wrapper object on every read, and the
    Control class defines no __eq__, so two reads of one control are
    never the same object. wh-spoken-phrase-select.6.1.
    """

    def test_a_second_wrapper_for_the_same_control_does_not_refuse(self):
        """The product path reads focus again through
        auto.GetFocusedControl and receives a new wrapper object for the
        same control. That must not refuse the command."""
        control = _control_that_finds()
        second_read = _control_that_finds()
        assert second_read is not control
        outcome, _ = _select(control, "brown fox", focus=lambda: second_read)
        assert outcome == PHRASE_SELECTED

    def test_reports_focus_changed(self):
        control = _control_that_finds()
        other = _control_that_finds(runtime_id=(7, 7, 7))
        outcome, matched = _select(control, "brown fox", focus=lambda: other)
        assert outcome == PHRASE_FOCUS_CHANGED
        assert matched is None

    def test_selects_nothing_when_focus_changed(self):
        control = _control_that_finds()
        other = _control_that_finds(runtime_id=(7, 7, 7))
        _select(control, "brown fox", focus=lambda: other)
        assert not control._found_range.Select.called


class TestFocusCannotBeChecked:
    """The check itself could not run. The command must do nothing.

    The user decided this on 2026-08-19, answering
    wh-spoken-phrase-select.7.2. A select against a control that may no
    longer hold the focus can act on a document the user is no longer
    looking at, and it leaves a selection behind that the next edit
    command acts on. The user cannot see that happen. A command that
    does nothing is the smaller cost.

    ui/strategies/specific.py lines 356 to 392 already answers the same
    question the same way, decided as wh-ix1z.14.

    Four things can stop the check: the focus read raises, the focus
    read returns nothing, a runtime id cannot be read, and a runtime id
    comes back empty. All four give PHRASE_FOCUS_UNKNOWN, which is a
    different answer from PHRASE_FOCUS_CHANGED. The two stay apart
    because wh-spoken-phrase-select.3 must tell the user two different
    things.
    """

    def test_a_focus_read_that_raises_selects_nothing(self):
        control = _control_that_finds()

        def _raises():
            raise OSError("busy")

        outcome, matched = _select(control, "brown fox", focus=_raises)
        assert outcome == PHRASE_FOCUS_UNKNOWN
        assert matched is None
        assert not control._found_range.Select.called

    def test_a_focus_read_that_returns_nothing_selects_nothing(self):
        control = _control_that_finds()
        outcome, matched = _select(control, "brown fox", focus=lambda: None)
        assert outcome == PHRASE_FOCUS_UNKNOWN
        assert matched is None
        assert not control._found_range.Select.called

    def test_a_runtime_id_that_cannot_be_read_selects_nothing(self):
        control = _control_that_finds()
        other = _control_that_finds(runtime_id=(7, 7, 7))
        other.GetRuntimeId.side_effect = OSError("busy")
        outcome, matched = _select(control, "brown fox", focus=lambda: other)
        assert outcome == PHRASE_FOCUS_UNKNOWN
        assert matched is None
        assert not control._found_range.Select.called

    def test_an_empty_runtime_id_selects_nothing(self):
        control = _control_that_finds()
        other = _control_that_finds(runtime_id=())
        outcome, matched = _select(control, "brown fox", focus=lambda: other)
        assert outcome == PHRASE_FOCUS_UNKNOWN
        assert matched is None
        assert not control._found_range.Select.called

    def test_an_unreadable_id_on_the_captured_control_selects_nothing(self):
        """The captured control is the other half of the comparison. An
        unreadable id there stops the check just the same."""
        control = _control_that_finds()
        control.GetRuntimeId.side_effect = OSError("busy")
        second_read = _control_that_finds()
        outcome, matched = _select(control, "brown fox", focus=lambda: second_read)
        assert outcome == PHRASE_FOCUS_UNKNOWN
        assert matched is None
        assert not control._found_range.Select.called

    def test_the_refusal_never_logs_at_error(self, caplog):
        """An ERROR record shows the user a notification box. This
        refusal must not."""
        control = _control_that_finds()
        with caplog.at_level(logging.DEBUG):
            _select(control, "brown fox", focus=lambda: None)
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


class TestEmptyPhrase:
    """The action already declines an empty capture. The module must not
    search for nothing if a later caller forgets."""

    @pytest.mark.parametrize("phrase", ["", "   ", None])
    def test_empty_phrase_finds_nothing(self, phrase):
        control = _control_that_finds()
        outcome, _ = _select(control, phrase)
        assert outcome == PHRASE_NOT_FOUND
        assert not control._document_range.FindText.called
