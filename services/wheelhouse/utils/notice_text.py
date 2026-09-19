"""Fit Windows notice text inside the fields plyer writes it into.

wh-notice-length-guard. A Windows notice that is too long does not
appear at all, and nothing in the log says why. This module is the one
place that measures the text and shortens it, so a long notice loses its
tail instead of losing the whole notice.

HOW THE LOSS HAPPENS. plyer delivers a notice by filling one
NOTIFYICONDATAW structure whose text fields are fixed-size wide-character
arrays, and it does that on a thread it starts itself
(plyer/platforms/win/notification.py:17). Assigning a longer string makes
ctypes raise ValueError on that thread. Nothing in WheelHouse sees the
exception, so the user gets silence.

THE FIELDS, read from plyer/platforms/win/libs/win_api_defs.py:
    line 59  szTip        WCHAR * 128   receives app_name
    line 62  szInfo       WCHAR * 256   receives message
    line 64  szInfoTitle  WCHAR * 64    receives title
plyer/platforms/win/libs/balloontip.py:179-184 fills them positionally,
which is what ties each keyword argument to each field.

WHAT THE LIMITS COUNT. A WCHAR is two bytes, so these arrays hold
UTF-16 code units, not Python characters. The two counts agree for every
character up to U+FFFF and part company above it, where one character
becomes a surrogate pair and takes two slots. A message of 255 Python
characters holding two emoji is 257 units, and the assignment raises --
which is how wh-notice-length-guard.2.1 found this module measuring the
wrong thing after its first three commits. Everything below is counted
in wide characters.

WHY THE LIMITS ARE ONE BELOW EACH ARRAY. ctypes accepts a string exactly
as long as the array and raises at one more: measured on 2026-09-06,
szInfo took 256 and raised at 257, szInfoTitle took 64 and raised at 65.
Win32 treats both fields as text plus a terminating NUL, so the largest
count safe under both readings is one below the array size. Keeping the
margin also means a future plyer that writes the terminator itself needs
no change here.

WHY app_name IS NOT GUARDED. Every caller in WheelHouse passes a literal
of at most 20 characters ("Wheelhouse Telemetry"), against a 128-character
field. There is no value to measure.

WHY NOT textwrap.shorten. The built-in collapses every run of whitespace
into a single space, which would join the lines of a multi-line notice.
Several notices put their detail on a second line, so that would change
what the user reads in the ordinary case to guard the rare one.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# One below szInfo's WCHAR * 256, so the text plus a terminating NUL fits.
MAX_MESSAGE_WIDE_CHARACTERS = 255

# One below szInfoTitle's WCHAR * 64, for the same reason.
MAX_TITLE_WIDE_CHARACTERS = 63

# Three ASCII periods rather than the single ellipsis character: the
# notice is rendered by Windows from a wide-character field, but the same
# text is written to the log, and a CP1252 console cannot print U+2026.
ELLIPSIS = "..."


def _wide_characters(text: str) -> int:
    """Count the wide characters Windows stores for this text.

    A NOTIFYICONDATAW field is an array of WCHAR, which is two bytes on
    Windows, so it counts UTF-16 code units. Python's len() counts code
    points. The two agree for every character up to U+FFFF and disagree
    above it, where one code point becomes a surrogate pair and takes
    two WCHAR slots.
    """
    return len(text.encode("utf-16-le")) // 2


def _longest_prefix_within(text: str, budget: int) -> str:
    """Return the longest prefix of text that fits budget wide characters.

    Whole code points are kept. Cutting at a wide-character count
    instead would land between the two halves of a supplementary
    character and produce a lone surrogate, which cannot be encoded at
    all -- a worse failure than the one this module exists to stop.
    """
    used = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > budget:
            return text[:index]
        used += width
    return text


def _fit_one_field(text: str, limit: int, field_name: str) -> str:
    """Return text that fits limit, and log the whole of anything cut.

    ``limit`` is a count of WIDE CHARACTERS, not of Python characters.
    Measuring in Python characters is what wh-notice-length-guard.2.1
    reported: a message of 255 code points holding two emoji is 257
    wide characters, so ctypes rejected it on plyer's own thread and
    the notice was lost with no line to say why -- through text this
    function had already measured and passed.

    Non-string input is returned unchanged. This function measures
    length; deciding what a caller may pass is the caller's own job, and
    raising TypeError here would drop the notice this module exists to
    keep.
    """
    if not isinstance(text, str):
        return text

    width = _wide_characters(text)
    if width <= limit:
        return text

    kept = _longest_prefix_within(
        text, limit - _wide_characters(ELLIPSIS)
    ) + ELLIPSIS
    logger.warning(
        "Notice %s field was %d wide characters, over the %d the Windows "
        "notification structure holds; it was shortened to %d and the "
        "whole text follows. Original: %s",
        field_name, width, limit, _wide_characters(kept), text,
    )
    return kept


def fit_notice_text(title: str, message: str) -> tuple[str, str]:
    """Return (title, message) shortened to what plyer's fields hold.

    Call this immediately before plyer.notification.notify and pass the
    result through. Text that already fits is returned unchanged and
    logs nothing.
    """
    return (
        _fit_one_field(title, MAX_TITLE_WIDE_CHARACTERS, "title"),
        _fit_one_field(message, MAX_MESSAGE_WIDE_CHARACTERS, "message"),
    )


def send_notice(
    title: str,
    message: str,
    *,
    app_name: str | None = None,
    timeout: int | None = None,
) -> bool:
    """Send one Windows notice, with both text fields fitted first.

    This is the only place in WheelHouse that reaches plyer, so it is
    the only place a notice can be too long to appear. The property is
    held by tests/test_utils/
    test_notice_sender_is_the_only_plyer_caller.py.

    Returns True when plyer was asked to deliver the notice and False
    when there is no backend to ask -- plyer absent, or its notify
    attribute missing or not callable. Six of the eleven call sites
    read that value. Five of them log their own "notification service
    unavailable" line on False (gui.py show_notification, gui.py
    click_first_use_hint, ui/ui_action_handler.py, utils/monitors.py,
    utils/speech_notifier.py), and utils/notifier_worker.py reads it
    without logging, to keep its delivered count honest. The remaining
    five ignore it, which is why this function writes its own WARNING
    as well: at those five a missing backend would otherwise leave no
    record at all, and a notice lost in silence is the whole reason
    this module exists.

    EXCEPTIONS FROM THE DELIVERY ITSELF ARE NOT CAUGHT. Every caller
    wraps its call in its own try/except and logs what went wrong there;
    swallowing here would make all of those handlers unreachable and
    would hide a real delivery failure behind a silent True. Only the
    import is treated as an availability question, because three callers
    already imported plyer inside the function for exactly that reason,
    and one of them (ui/ui_action_handler.py) has a test that removes
    plyer from sys.modules.

    ``app_name`` and ``timeout`` are left out of the call when they are
    None, so a caller that never passed them keeps plyer's own defaults
    rather than acquiring WheelHouse's opinion of them.
    """
    try:
        from plyer import notification
    except Exception as exc:
        # The exception type is what separates "plyer is not installed"
        # from "plyer is installed and raises on import". Both produce
        # False, and a reader who only sees False cannot tell them
        # apart or know where to look.
        logger.warning(
            "Notice not sent: importing plyer raised %s: %s. Title: %s",
            type(exc).__name__, exc, title,
        )
        return False

    if not callable(getattr(notification, "notify", None)):
        logger.warning(
            "Notice not sent: plyer imported but its notify attribute is "
            "%r, which is not callable. Title: %s",
            getattr(notification, "notify", None), title,
        )
        return False

    fitted_title, fitted_message = fit_notice_text(title, message)
    arguments: dict[str, object] = {
        "title": fitted_title,
        "message": fitted_message,
    }
    if app_name is not None:
        arguments["app_name"] = app_name
    if timeout is not None:
        arguments["timeout"] = timeout

    # The callable() check above is what proves this is safe; a type
    # checker cannot follow it, because plyer reaches its backend
    # through a proxy whose notify is declared Optional.
    notification.notify(**arguments)  # type: ignore[misc]
    return True
