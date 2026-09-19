"""Tests for ui/phrase_select_notice.py -- tell the user what happened.

wh-spoken-phrase-select.3. The select phrase command can end six ways,
and five of them change nothing on the screen. A person who cannot see
the screen learns nothing from that. The module under test turns an
outcome into a Windows notification, which a screen reader reads.

The user approved three messages on 2026-08-19, grouped by what the
person should do next: say different words, move somewhere else, or
repeat the command.

No test here sends a real notification. Every test passes its own
worker.
"""
import logging
from unittest.mock import MagicMock

import pytest

from ui.phrase_select_notice import (
    NOTICE_TITLE,
    message_for,
    notify_outcome,
)
from ui.uia_phrase_select import (
    PHRASE_FAILED,
    PHRASE_FOCUS_CHANGED,
    PHRASE_FOCUS_UNKNOWN,
    PHRASE_NOT_FOUND,
    PHRASE_NO_CONTROL,
    PHRASE_NO_TEXT_PATTERN,
    PHRASE_SELECTED,
)


class TestMessageForEachOutcome:
    """One message per outcome, in the wording the user approved."""

    def test_a_select_that_worked_says_nothing(self):
        assert message_for(PHRASE_SELECTED, "brown fox") is None

    def test_words_that_are_absent_name_the_words(self):
        assert message_for(PHRASE_NOT_FOUND, "brown fox") == (
            "Not found: brown fox"
        )

    @pytest.mark.parametrize(
        "outcome", [PHRASE_NO_CONTROL, PHRASE_NO_TEXT_PATTERN]
    )
    def test_a_place_that_cannot_select_says_so(self, outcome):
        assert message_for(outcome, "brown fox") == (
            "Cannot select text here"
        )

    @pytest.mark.parametrize(
        "outcome",
        [PHRASE_FOCUS_CHANGED, PHRASE_FOCUS_UNKNOWN, PHRASE_FAILED],
    )
    def test_a_failure_asks_for_another_try(self, outcome):
        assert message_for(outcome, "brown fox") == (
            "Select failed, try again"
        )

    def test_an_outcome_nobody_planned_still_reports(self):
        """A new outcome must not silently show nothing.

        A later change can add a seventh outcome. The safe default is
        the message that asks the person to repeat the command.
        """
        assert message_for("a_new_outcome", "brown fox") == (
            "Select failed, try again"
        )

    def test_an_empty_phrase_does_not_leave_a_dangling_colon(self):
        assert message_for(PHRASE_NOT_FOUND, "") == "Not found"


class TestNotifyOutcome:
    """The submitted payload, and the cases that submit nothing."""

    def test_a_select_that_worked_submits_nothing(self):
        worker = MagicMock()
        assert notify_outcome(PHRASE_SELECTED, "brown fox", worker) is False
        assert not worker.submit.called

    def test_the_payload_carries_the_title_and_the_message(self):
        worker = MagicMock()
        worker.submit.return_value = True

        assert notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker) is True

        payload = worker.submit.call_args[0][0]
        assert payload.title == NOTICE_TITLE
        assert payload.message == "Not found: brown fox"

    def test_the_payload_never_claims_to_be_an_error(self):
        """ERROR in the level name is how a false error reaches the log.

        The notifier worker does not read levelname, but a later reader
        of the payload might. wh-focus-refusal-popup is the case where
        an ERROR record told the user about a success.
        """
        worker = MagicMock()
        notify_outcome(PHRASE_FAILED, "brown fox", worker)

        payload = worker.submit.call_args[0][0]
        assert payload.levelname == "INFO"

    def test_no_worker_shows_nothing_and_raises_nothing(self):
        assert notify_outcome(PHRASE_NOT_FOUND, "brown fox", None) is False

    def test_a_worker_that_raises_does_not_reach_the_caller(self):
        worker = MagicMock()
        worker.submit.side_effect = RuntimeError("queue is gone")

        assert notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker) is False

    def test_a_worker_that_refuses_the_payload_reports_false(self):
        worker = MagicMock()
        worker.submit.return_value = False

        assert notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker) is False

    def test_a_refused_payload_leaves_a_trace_in_the_log(self, caplog):
        """A full queue must not lose the report without a trace.

        wh-spoken-phrase-select.8.1. The worker refuses a payload when
        its queue holds 64 entries already. The other two paths that
        show nothing each write a line. This one wrote nothing, so a
        command that reported nothing looked in wheelhouse.log exactly
        like a command that reported.
        """
        worker = MagicMock()
        worker.submit.return_value = False

        with caplog.at_level(logging.DEBUG):
            notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker)

        assert "not shown" in caplog.text
        assert PHRASE_NOT_FOUND in caplog.text

    def test_a_refused_payload_does_not_log_at_error(self, caplog):
        """An ERROR record would show a second notification of its own."""
        worker = MagicMock()
        worker.submit.return_value = False

        with caplog.at_level(logging.DEBUG):
            notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker)

        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class TestTheWordsStayOutOfTheLog:
    """wh-797.17. The words a person speaks are theirs."""

    def test_a_worker_that_raises_never_logs_the_phrase(self, caplog):
        worker = MagicMock()
        worker.submit.side_effect = RuntimeError("queue is gone")

        with caplog.at_level(logging.DEBUG):
            notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker)

        assert "brown fox" not in caplog.text

    def test_a_refused_payload_never_logs_the_phrase(self, caplog):
        worker = MagicMock()
        worker.submit.return_value = False

        with caplog.at_level(logging.DEBUG):
            notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker)

        assert "brown fox" not in caplog.text

    def test_no_worker_never_logs_the_phrase(self, caplog):
        with caplog.at_level(logging.DEBUG):
            notify_outcome(PHRASE_NOT_FOUND, "brown fox", None)

        assert "brown fox" not in caplog.text

    def test_nothing_logs_at_error(self, caplog):
        """An ERROR record pops a second notification of its own.

        ErrorNotificationHandler (utils/error_notifier.py) listens at
        ERROR level. An ERROR here would show the user two boxes for
        one command, and the second one would say [ERROR].
        """
        worker = MagicMock()
        worker.submit.side_effect = RuntimeError("queue is gone")

        with caplog.at_level(logging.DEBUG):
            notify_outcome(PHRASE_NOT_FOUND, "brown fox", worker)

        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
