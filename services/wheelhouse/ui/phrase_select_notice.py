"""Tell the user what the select phrase command did.

wh-spoken-phrase-select.3. The command ends six ways, and five of them
change nothing on the screen. A person who cannot see the screen learns
nothing from a screen that did not change, so the command has to say
something.

WHY A WINDOWS NOTIFICATION. The user decided this on 2026-08-19. A
person who cannot see the screen runs a screen reader, and a screen
reader announces a Windows notification. Wheelhouse needs no voice of
its own and no new window. The Input process already owns a
NotifierWorker, because input_proc.py calls setup_logging, so the
message never crosses a process boundary.

WHY NOT A LOG RECORD AT ERROR. ErrorNotificationHandler in
utils/error_notifier.py listens at ERROR level and shows a notification
for every ERROR record. Reaching the user that way costs three things.
The title comes out as "[ERROR] phrase_select_notice", which is wrong
for words that are simply absent. The record also goes into
wheelhouse.log as an error, which it is not. And the rate limit in that
handler keys on the exact message text. This module submits a payload
to the worker directly and avoids all three.

WHY THREE MESSAGES AND NOT SIX. The user approved a grouping by what
the person should do next: say different words, move somewhere else, or
repeat the command. Two outcomes that need the same action do not need
two messages.

NO RATE LIMIT. Say the same command three times and you get three
notifications. The rate limit lives in ErrorNotificationHandler, not in
the worker, so this path skips it. That is deliberate: a repeat is the
normal way to retry, and a suppressed second answer would look like the
command did nothing at all.

EVERY PATH THAT SHOWS NOTHING WRITES A LINE. The report can go missing
three ways: no worker exists, the worker refuses the payload because its
queue already holds 64 entries, and submit raises. Each one writes a
line to wheelhouse.log, so a command that reported nothing does not read
like a command that reported (wh-spoken-phrase-select.8.1). One case
stays invisible from here: NotifierWorker.submit still accepts a payload
after someone stops the worker, and returns True. Only
utils/notifier_worker.py can tell that case apart, and that file also
serves ErrorNotificationHandler.

THE WORDS NEVER REACH THE LOG. "Not found: brown fox" goes to the
screen, where the person who spoke the words is the only reader. No log
line in this module contains the phrase (wh-797.17).
"""
import logging
from typing import Optional

from utils.notifier_worker import NotifierPayload

from .uia_phrase_select import (
    PHRASE_FOCUS_CHANGED,
    PHRASE_FOCUS_UNKNOWN,
    PHRASE_NOT_FOUND,
    PHRASE_NO_CONTROL,
    PHRASE_NO_TEXT_PATTERN,
    PHRASE_SELECTED,
)

logger = logging.getLogger(__name__)

# The notification title. Plain, because the person hears it read out
# before the message.
NOTICE_TITLE = "Wheelhouse"

CANNOT_SELECT_HERE = "Cannot select text here"
TRY_AGAIN = "Select failed, try again"
NOT_FOUND_PREFIX = "Not found"

# The outcomes that share a message. PHRASE_FAILED is absent on purpose:
# it takes the default below, and so does any outcome added later.
_MESSAGES = {
    PHRASE_NO_CONTROL: CANNOT_SELECT_HERE,
    PHRASE_NO_TEXT_PATTERN: CANNOT_SELECT_HERE,
    PHRASE_FOCUS_CHANGED: TRY_AGAIN,
    PHRASE_FOCUS_UNKNOWN: TRY_AGAIN,
}


def message_for(outcome: str, phrase: str) -> Optional[str]:
    """The sentence to show the user, or None when the select worked.

    An outcome that this module does not know still produces a message.
    A later change can add a seventh outcome, and a silent seventh
    outcome would take the user back to a screen that says nothing.

    Args:
        outcome: One of the PHRASE_ constants in ui/uia_phrase_select.py.
        phrase: The words the user spoke.

    Returns:
        The message, or None when nothing needs saying.
    """
    if outcome == PHRASE_SELECTED:
        return None
    if outcome == PHRASE_NOT_FOUND:
        if not phrase:
            return NOT_FOUND_PREFIX
        return f"{NOT_FOUND_PREFIX}: {phrase}"
    return _MESSAGES.get(outcome, TRY_AGAIN)


def notify_outcome(outcome: str, phrase: str, worker) -> bool:
    """Show a Windows notification unless the select worked.

    The caller passes the worker. ui/ui_action_handler.py reads it from
    utils.logging_setup.get_notifier_worker(), which returns None when
    nothing called setup_logging. A missing worker is not an error here:
    the command still did its job, and only the report is lost.

    Args:
        outcome: One of the PHRASE_ constants in ui/uia_phrase_select.py.
        phrase: The words the user spoke.
        worker: A NotifierWorker, or None.

    Returns:
        True when the worker accepted the payload.
    """
    message = message_for(outcome, phrase)
    if message is None:
        return False
    if worker is None:
        logger.info(
            "select_phrase: no notifier worker, outcome %s not shown", outcome
        )
        return False

    payload = NotifierPayload(
        title=NOTICE_TITLE,
        message=message,
        # Not ERROR. An ERROR record would show a second notification
        # through ErrorNotificationHandler, and that one would say
        # [ERROR]. wh-focus-refusal-popup is the case where an error
        # notification reported a success.
        levelname="INFO",
        trace_id="",
    )
    try:
        accepted = bool(worker.submit(payload))
    except Exception as exc:
        # The phrase never appears in this line (wh-797.17). WARNING,
        # not ERROR, because an ERROR here pops its own notification.
        logger.warning(
            "select_phrase: the notification failed: %s", type(exc).__name__
        )
        return False

    if not accepted:
        # The worker refuses a payload when its queue already holds 64
        # entries. Without this line the report vanished and left no
        # trace, which is the defect this whole module removes
        # (wh-spoken-phrase-select.8.1).
        logger.info(
            "select_phrase: the notifier refused the payload, outcome %s "
            "not shown",
            outcome,
        )
    return accepted
