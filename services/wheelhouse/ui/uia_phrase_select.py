"""Find a spoken phrase in the focused text control and select it.

wh-spoken-phrase-select. The user speaks words, and Wheelhouse selects
the first place those words appear in the document. The search and the
selection both run inside the target application, through the Windows UI
Automation text pattern. The document text never leaves that application
and never crosses a process boundary.

WHAT THIS MODULE DOES NOT DO, AND WHY.

It does not read the document. A tolerant match would need the whole
text, so that Wheelhouse could compare a normalized copy against the
spoken words. Word truncates a whole-document read at 65,000 characters
while its own search covers the whole document: a probe on 2026-08-18
found a phrase at about character 130,000 of a 21,984-word file that
read back as exactly 65,000 characters. A tolerant match therefore
cannot work in Word, so this module matches exactly instead. Letter case
is the one difference the search forgives, because the library ignores
case for free.

It does not offer a second match. The first match wins. Numbered
bubbles over every match were the largest block of work in
docs/design/spoken-phrase-commands.md, and nothing measured says how
often a person wants the second one.

It does not start from the cursor. The search starts at the top of the
document, because the cursor position is one more thing to get right and
nothing measured says it helps.

It does not select when it cannot prove the focus stayed put. The user
decided this on 2026-08-19, answering wh-spoken-phrase-select.7.2. A
select against a control that may no longer hold the focus can act on a
document the user is no longer looking at, and it leaves a selection
behind that the next edit command acts on. The user cannot see that
happen. A command that does nothing costs the user one repeat.
ui/strategies/specific.py lines 356 to 392 answers the same question the
same way, decided as wh-ix1z.14.

MEASURED COST. The search took 0.09 ms in Notepad and 14.40 ms in a
72-page Word document. The select took 4.31 ms in Notepad and 2.18 ms in
Word, with waitTime=0. The library default sleeps 0.5 s after a select,
which is why every call here passes waitTime=0.

WHAT THE PROBE PROVED, ON 2026-08-19. A following command acts on
exactly the phrase this module selects. Ten destructive runs in Notepad
removed the selected words and nothing else. In every run the range
still held the same text at the moment of acting, the selection was
still the phrase, and focus was still the same control. A five second
delay changed none of that. Word behaves the same, except that Word
removes one adjoining space along with a deleted selection, which is
Word's smart cut and paste setting.
"""

import logging
from typing import Any, Callable, Optional, Tuple

import uiautomation as auto

logger = logging.getLogger(__name__)

# The outcomes. ui/ui_action_handler.py hands each one to
# ui/phrase_select_notice.py, which shows the user a Windows
# notification (wh-spoken-phrase-select.3).
PHRASE_SELECTED = "selected"
PHRASE_NOT_FOUND = "not_found"
PHRASE_NO_CONTROL = "no_control"
PHRASE_NO_TEXT_PATTERN = "no_text_pattern"
# focus_changed means the check ran and proved the focus moved.
# focus_unknown means the check could not run at all. The user sees the
# same message for both, because the same action fixes both. They stay
# apart so wheelhouse.log records which one happened.
PHRASE_FOCUS_CHANGED = "focus_changed"
PHRASE_FOCUS_UNKNOWN = "focus_unknown"
PHRASE_FAILED = "failed"


def read_focused_control() -> Any:
    """Read the control that holds the keyboard focus right now.

    This is the only thing the select needs. ui/context.py's
    capture_context() also reads the class name, the process id and the
    top level window, and this command uses none of them. Those extra
    reads cost time on the voice command path, and capture_context()
    logs an ERROR when one of them raises. Every ERROR record on the
    root logger shows the user a notification box, so a select that
    succeeds could still show the user an error.
    wh-spoken-phrase-select.7.1.
    """
    return auto.GetFocusedControl()


def _control_identity(control: Any) -> Optional[Tuple]:
    """A value that changes when the focus moves to another control.

    Two reads of one control are never the same Python object.
    uiautomation builds a new wrapper on every read: GetFocusedControl
    calls Control.CreateControlFromElement, which constructs a new
    instance every time, and the Control class defines no __eq__. The
    runtime id is the value that stays the same across the two reads.
    scripts/benchmarks/spoken_phrase_action_probe.py compares controls
    the same way. wh-spoken-phrase-select.6.1.

    Returns:
        The runtime id, or None when it cannot be read. None means the
        check cannot run, and the caller then selects nothing.
    """
    try:
        runtime_id = control.GetRuntimeId()
    except Exception:
        return None
    if not runtime_id:
        return None
    try:
        return tuple(runtime_id)
    except TypeError:
        return None


def select_phrase_in_control(
    focused_control: Any,
    phrase: Optional[str],
    focus_reader: Optional[Callable[[], Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Select the first match of ``phrase`` inside ``focused_control``.

    Args:
        focused_control: The control captured when the command arrived.
        phrase: The words the user spoke.
        focus_reader: Reads the focused control again, just before the
            select. Tests pass their own reader so that no test makes a
            real UI Automation call.

    Returns:
        A pair. The first item is one of the PHRASE_ constants above. The
        second item is the text the search matched, when the select
        succeeded and the text could be read, and None otherwise.
    """
    if focused_control is None:
        return PHRASE_NO_CONTROL, None

    if not phrase or not phrase.strip():
        return PHRASE_NOT_FOUND, None

    reader = focus_reader or read_focused_control

    try:
        text_pattern = focused_control.GetPattern(auto.PatternId.TextPattern)
        if not text_pattern:
            return PHRASE_NO_TEXT_PATTERN, None

        # FindText takes (text, backward, ignoreCase). Search forward from
        # the top of the document and forgive letter case.
        found = text_pattern.DocumentRange.FindText(phrase, False, True)
        if not found:
            return PHRASE_NOT_FOUND, None

        # The user can click somewhere else while the search runs. A
        # select against a control that no longer holds the focus would
        # act on a document the user is no longer looking at. The two
        # refusals below are the only two paths that reach a select.
        #
        # A check that cannot run refuses the command. The user decided
        # this on 2026-08-19, answering wh-spoken-phrase-select.7.2.
        # Four things stop the check: the focus read raises, the focus
        # read returns nothing, and either runtime id cannot be read.
        #
        # The reason is that the two mistakes cost different amounts. A
        # select against a control that may no longer hold the focus can
        # act on a document the user is no longer looking at, and it
        # leaves a selection behind that the next edit command acts on.
        # The user cannot see that happen. A command that does nothing
        # costs the user one repeat.
        #
        # ui/strategies/specific.py lines 356 to 392 already answers the
        # same question the same way, decided as wh-ix1z.14. Its comment
        # records that the other answer reopened the wh-ix1z.11 class of
        # bug on the failure path.
        #
        # Nothing here logs at ERROR. ErrorNotificationHandler in
        # utils/error_notifier.py is attached at ERROR level, so an
        # ERROR record shows the user a notification box. Neither
        # refusal deserves one.
        try:
            current = reader()
        except Exception:
            logger.info("select_phrase: the focus read failed, nothing selected")
            return PHRASE_FOCUS_UNKNOWN, None
        if current is None:
            logger.info("select_phrase: no focused control, nothing selected")
            return PHRASE_FOCUS_UNKNOWN, None

        before = _control_identity(focused_control)
        after = _control_identity(current)
        if before is None or after is None:
            logger.info(
                "select_phrase: the focus check could not run, nothing selected"
            )
            return PHRASE_FOCUS_UNKNOWN, None
        if before != after:
            logger.info("select_phrase: focus changed, nothing selected")
            return PHRASE_FOCUS_CHANGED, None

        # waitTime=0 removes the library's 0.5 s sleep. See MEASURED COST
        # in the module docstring.
        if not found.Select(waitTime=0):
            logger.info("select_phrase: the select call refused")
            return PHRASE_FAILED, None

        # The matched text only names the result for the caller and the
        # log. A read that fails must not turn a good selection into a
        # failure.
        try:
            matched = found.GetText(-1)
        except Exception:
            matched = None

        return PHRASE_SELECTED, matched

    except Exception as e:
        # The phrase never appears in this line. The words a person
        # speaks are theirs, and a log file is not the place for them.
        logger.info("select_phrase: UI Automation failed: %s", type(e).__name__)
        logger.debug("select_phrase: failure detail", exc_info=True)
        return PHRASE_FAILED, None
