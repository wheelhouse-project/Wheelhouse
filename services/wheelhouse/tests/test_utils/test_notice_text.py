"""Tests for the notice-length guard (wh-notice-length-guard).

WHY THIS FILE EXISTS. plyer delivers a Windows notice by filling one
NOTIFYICONDATAW structure whose text fields are fixed-size wide-character
arrays, and it does that on a thread of its own
(plyer/platforms/win/notification.py:17). A string longer than a field
makes ctypes raise ValueError on that thread, so the user sees no notice
and no log line says why. David met this on 2026-09-06.

THE THREE FIELDS AND THEIR SIZES, read from
plyer/platforms/win/libs/win_api_defs.py:
    line 59  szTip        WCHAR * 128   receives app_name
    line 62  szInfo       WCHAR * 256   receives message
    line 64  szInfoTitle  WCHAR * 64    receives title
plyer/platforms/win/libs/balloontip.py:179-184 fills them positionally,
which is what ties each argument to each field.

WHY THE LIMITS HERE ARE 255 AND 63, ONE BELOW EACH ARRAY. ctypes itself
accepts a string exactly as long as the array and raises at one more
(measured: 256 accepted, 257 raises). Win32 treats these fields as text
plus a terminating NUL, so the largest count safe under both readings is
one below the array size. The tests at the bottom of this file prove the
guard's output fits a real ctypes array of each size, so the constants
cannot drift away from the structure they exist to fit.
"""

import ctypes
import logging

import pytest

from utils.notice_text import (
    MAX_MESSAGE_WIDE_CHARACTERS,
    MAX_TITLE_WIDE_CHARACTERS,
    fit_notice_text,
    send_notice,
)


class TestMessageLength:
    """The message field, which lands in szInfo (256 wide characters)."""

    def test_a_message_over_the_limit_is_shortened_to_fit(self):
        """A 257-character message reaches plyer at or under 255.

        257 is the exact count at which ctypes raises for szInfo, so it
        is the shortest message that produces the silent failure.
        """
        title, message = fit_notice_text("Wheelhouse", "m" * 257)

        assert len(message) <= MAX_MESSAGE_WIDE_CHARACTERS
        assert title == "Wheelhouse"

    def test_the_shortened_message_ends_with_a_visible_ellipsis(self):
        """The reader can see that words were removed."""
        _, message = fit_notice_text("Wheelhouse", "m" * 257)

        assert message.endswith("...")

    def test_a_message_at_the_limit_passes_unchanged(self):
        """Exactly 255 characters is inside the limit and is not touched."""
        original = "m" * MAX_MESSAGE_WIDE_CHARACTERS

        _, message = fit_notice_text("Wheelhouse", original)

        assert message == original

    def test_a_message_one_over_the_limit_is_shortened(self):
        """256 characters is one too many, so the guard acts on it."""
        _, message = fit_notice_text("Wheelhouse", "m" * 256)

        assert len(message) <= MAX_MESSAGE_WIDE_CHARACTERS
        assert message != "m" * 256

    def test_a_real_notice_of_245_characters_passes_unchanged(self):
        """The length that produced this bead, written out.

        Every other length in this file is built from
        MAX_MESSAGE_WIDE_CHARACTERS, so lowering that constant moves
        those tests with it and they stay green. 245 is the
        measured length of the WinRT capture-failure notice with
        its provider prefix (wh-capture-winrt-required A10), and it
        is written here as a number so that a message limit set
        anywhere below it fails this test.
        """
        original = "m" * 245

        _, message = fit_notice_text("Wheelhouse", original)

        assert message == original


class TestTitleLength:
    """The title field, which lands in szInfoTitle (64 wide characters).

    This is the part the bead asked me to establish by reading plyer
    (A4): the title is limited too, and its limit is a quarter of the
    message's. A title is normally short, so the overflow here is the
    one a reader would not predict.
    """

    def test_a_title_over_the_limit_is_shortened_to_fit(self):
        title, _ = fit_notice_text("t" * 65, "a short message")

        assert len(title) <= MAX_TITLE_WIDE_CHARACTERS

    def test_a_title_at_the_limit_passes_unchanged(self):
        original = "t" * MAX_TITLE_WIDE_CHARACTERS

        title, _ = fit_notice_text(original, "a short message")

        assert title == original

    def test_a_long_title_does_not_disturb_the_message(self):
        """Shortening one field leaves the other alone."""
        _, message = fit_notice_text("t" * 200, "a short message")

        assert message == "a short message"


class TestTheOverflowIsReported:
    """No notice is dropped, and nothing is lost silently."""

    def test_an_overflowing_message_is_logged_at_warning(self, caplog):
        """One WARNING, and it is about the message.

        The field name is asserted here as well as in the test
        below. Counting alone is not enough: the title is measured
        in the same call, so a defect that shortens the title
        instead of the message still leaves exactly one record.
        """
        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text("Wheelhouse", "m" * 257)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "message" in warnings[0].getMessage()

    def test_the_warning_names_the_field_and_the_measured_length(self, caplog):
        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text("Wheelhouse", "m" * 257)

        text = caplog.records[0].getMessage()
        assert "message" in text
        assert "257" in text

    def test_the_warning_carries_the_whole_text_that_was_shortened(self, caplog):
        """The log is where the removed words survive."""
        original = "m" * 257

        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text("Wheelhouse", original)

        assert original in caplog.records[0].getMessage()

    def test_an_overflowing_title_is_logged_at_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text("t" * 65, "a short message")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "title" in warnings[0].getMessage()

    def test_a_notice_that_fits_logs_nothing(self, caplog):
        """The ordinary case must stay silent, or the log fills up."""
        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text("Wheelhouse", "a short message")

        assert caplog.records == []


class TestTheOutputFitsTheRealStructure:
    """Tie the constants to the ctypes arrays they exist to fit.

    These are the tests that fail if somebody raises a constant to the
    array size. They build the same fixed-size arrays plyer builds and
    assign the guard's output, which is the operation that raises in
    production.
    """

    @pytest.mark.parametrize("length", [255, 256, 257, 300, 5000])
    def test_the_message_the_guard_returns_fits_szinfo(self, length):
        _, message = fit_notice_text("Wheelhouse", "m" * length)

        field = (ctypes.c_wchar * 256)()
        field.value = message  # raises ValueError if the guard let it through

        assert field.value == message

    @pytest.mark.parametrize("length", [63, 64, 65, 200])
    def test_the_title_the_guard_returns_fits_szinfotitle(self, length):
        title, _ = fit_notice_text("t" * length, "a short message")

        field = (ctypes.c_wchar * 64)()
        field.value = title

        assert field.value == title


class TestTheOutputFitsInUtf16Units:
    """Windows counts UTF-16 code units; Python len() counts code points.

    wh-notice-length-guard.2.1. A character above U+FFFF is one code
    point and TWO wide characters, so a message of 255 code points can
    be 257 wide characters. The array plyer assigns it to holds 256, and
    the assignment raises on plyer's own thread -- the same silent loss
    this module exists to stop, reached through text the guard has
    already measured and passed.

    Every test here builds the real ctypes array and assigns to it,
    because that assignment is the operation that raises in production.
    Two emoji is the smallest amount of non-BMP text that overflows a
    field whose code-point count is at the limit.
    """

    TWO_EMOJI = "\U0001F600\U0001F600"

    @staticmethod
    def _units(text: str) -> int:
        """Count the wide characters Windows will store."""
        return len(text.encode("utf-16-le")) // 2

    def test_a_message_whose_code_points_fit_but_units_do_not_is_cut(self):
        """255 code points, 257 wide characters: the guard must act."""
        original = "a" * 253 + self.TWO_EMOJI

        _, message = fit_notice_text("Wheelhouse", original)

        assert self._units(message) <= MAX_MESSAGE_WIDE_CHARACTERS

    def test_that_message_fits_the_real_szinfo_array(self):
        _, message = fit_notice_text("Wheelhouse", "a" * 253 + self.TWO_EMOJI)

        field = (ctypes.c_wchar * 256)()
        field.value = message  # raises ValueError if the guard let it through

        assert field.value == message

    def test_a_title_whose_code_points_fit_but_units_do_not_is_cut(self):
        """63 code points, 65 wide characters."""
        title, _ = fit_notice_text("t" * 61 + self.TWO_EMOJI, "short")

        assert self._units(title) <= MAX_TITLE_WIDE_CHARACTERS

    def test_that_title_fits_the_real_szinfotitle_array(self):
        title, _ = fit_notice_text("t" * 61 + self.TWO_EMOJI, "short")

        field = (ctypes.c_wchar * 64)()
        field.value = title

        assert field.value == title

    def test_text_the_guard_shortens_is_measured_in_units_too(self):
        """The shortening branch has the same defect as the pass-through.

        This text is far over the limit by either measure, so the guard
        shortens it. Counting the kept prefix in code points leaves 257
        wide characters, which still raises.
        """
        original = self.TWO_EMOJI + "b" * 290

        _, message = fit_notice_text("Wheelhouse", original)

        assert self._units(message) <= MAX_MESSAGE_WIDE_CHARACTERS
        field = (ctypes.c_wchar * 256)()
        field.value = message
        assert field.value == message

    def test_a_shortened_title_is_measured_in_units_too(self):
        original = self.TWO_EMOJI + "u" * 100

        title, _ = fit_notice_text(original, "short")

        assert self._units(title) <= MAX_TITLE_WIDE_CHARACTERS
        field = (ctypes.c_wchar * 64)()
        field.value = title
        assert field.value == title

    def test_a_message_that_is_all_non_bmp_text_fits(self):
        """Every character costs two, so the kept text is about half."""
        _, message = fit_notice_text("Wheelhouse", "\U0001F600" * 200)

        assert self._units(message) <= MAX_MESSAGE_WIDE_CHARACTERS
        field = (ctypes.c_wchar * 256)()
        field.value = message
        assert field.value == message

    def test_the_guard_never_splits_a_supplementary_character(self):
        """Half of an emoji is a lone surrogate, which cannot be encoded.

        Cutting the text at a wide-character count rather than a code
        point is the obvious way to write this fix and the wrong one: it
        can land between the two halves of one character. Encoding the
        result is what proves it did not happen.

        251 is chosen so the cut falls inside a character. The budget is
        252 wide characters, which is 255 less the three the ellipsis
        needs, so after 251 ASCII characters one slot is left and the
        next character needs two. A cut that ignores that keeps the
        first half of the emoji and nothing else.
        """
        _, message = fit_notice_text(
            "Wheelhouse", "a" * 251 + "\U0001F600" * 10
        )

        message.encode("utf-16-le")  # raises on a lone surrogate
        assert all(
            not 0xD800 <= ord(character) <= 0xDFFF
            for character in message
        )

    def test_an_overflow_measured_only_in_units_is_reported(self, caplog):
        """A reader must still be told, and still get the whole text."""
        original = "a" * 253 + self.TWO_EMOJI

        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text("Wheelhouse", original)

        assert len(caplog.records) == 1
        reported = caplog.records[0].getMessage()
        assert original in reported
        # 257 is the count the field uses; the code-point count is 255,
        # and reporting that one tells the reader the text fit.
        assert "257" in reported


class TestInputThatIsNotText:
    """A caller can hand the guard a value that is not a string.

    gui.py:1794 reads its message out of a queue payload with
    ``message.get("message", "")``, so a payload whose key holds None
    reaches the guard as None. Raising here would drop the notice this
    module exists to keep, so the value is passed through and the
    caller's own validation decides.
    """

    def test_a_message_that_is_not_text_passes_through(self):
        _, message = fit_notice_text("Wheelhouse", None)

        assert message is None

    def test_a_title_that_is_not_text_passes_through(self):
        title, _ = fit_notice_text(None, "a short message")

        assert title is None

    def test_a_value_that_is_not_text_logs_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            fit_notice_text(None, None)

        assert caplog.records == []


def _plyer_field_sizes():
    """Wide-character capacity of each text field, read from plyer itself.

    Reading plyer's own field table is what stops the constants in
    utils/notice_text.py from drifting away from the structure they
    exist to fit. A hand-written number here would agree with the guard
    forever, including when both are wrong.
    """
    from plyer.platforms.win.libs.win_api_defs import NOTIFYICONDATAW_fields

    return {
        name: ctypes.sizeof(field_type) // ctypes.sizeof(ctypes.c_wchar)
        for name, field_type in NOTIFYICONDATAW_fields
        if name.startswith("sz")
    }


class TestTheLimitsMatchPlyersOwnStructure:
    """Pin each constant to plyer's field table, not to itself.

    Every other test in this file reads MAX_MESSAGE_WIDE_CHARACTERS or
    MAX_TITLE_WIDE_CHARACTERS, so a change to a constant moves those tests
    with it and they stay green. These two do not: they compute the
    expected value from plyer's NOTIFYICONDATAW field sizes.
    """

    def test_the_message_limit_is_one_below_szinfo(self):
        assert MAX_MESSAGE_WIDE_CHARACTERS == _plyer_field_sizes()["szInfo"] - 1

    def test_the_title_limit_is_one_below_szinfotitle(self):
        assert MAX_TITLE_WIDE_CHARACTERS == _plyer_field_sizes()["szInfoTitle"] - 1

    def test_app_name_needs_no_guard_because_sztip_is_far_larger(self):
        """The reason utils/notice_text.py measures two fields, not three.

        Every WheelHouse caller passes a literal app_name; the longest
        is "Wheelhouse Telemetry" at utils/code_telemetry.py:90.
        """
        assert len("Wheelhouse Telemetry") < _plyer_field_sizes()["szTip"]


# ----------------------------------------------------------------------
# send_notice: the one route from WheelHouse to plyer.
# ----------------------------------------------------------------------


@pytest.fixture
def delivered(monkeypatch):
    """Record what reaches plyer, and keep the real backend untouched.

    plyer.notification is a lazy proxy; replacing its notify attribute
    is what the e2e suite already does, and it works through any
    reference to the proxy, including the one send_notice takes inside
    the call.
    """
    import plyer

    calls = []
    monkeypatch.setattr(
        plyer.notification, "notify", lambda **kw: calls.append(kw)
    )
    return calls


class TestTheSenderFitsWhatItSends:
    """The point of routing every notice through one function."""

    def test_a_message_over_the_limit_reaches_plyer_shortened(self, delivered):
        send_notice("Title", "m" * (MAX_MESSAGE_WIDE_CHARACTERS + 2))

        assert len(delivered[0]["message"]) == MAX_MESSAGE_WIDE_CHARACTERS

    def test_a_title_over_the_limit_reaches_plyer_shortened(self, delivered):
        send_notice("t" * (MAX_TITLE_WIDE_CHARACTERS + 2), "Message")

        assert len(delivered[0]["title"]) == MAX_TITLE_WIDE_CHARACTERS

    def test_text_that_already_fits_reaches_plyer_unchanged(self, delivered):
        send_notice("Wheelhouse", "Restarting application...")

        assert delivered[0]["title"] == "Wheelhouse"
        assert delivered[0]["message"] == "Restarting application..."

    def test_what_plyer_receives_fits_the_real_structure(self, delivered):
        send_notice(
            "t" * (MAX_TITLE_WIDE_CHARACTERS + 40),
            "m" * (MAX_MESSAGE_WIDE_CHARACTERS + 40),
        )

        # The assignment that raises in production, run on the text
        # plyer was actually handed.
        message_field = (ctypes.c_wchar * 256)()
        message_field.value = delivered[0]["message"]
        title_field = (ctypes.c_wchar * 64)()
        title_field.value = delivered[0]["title"]


class TestTheSenderSaysWhetherItSent:
    """Six of the eleven call sites read this value; five ignore it."""

    def test_it_reports_true_when_plyer_took_the_notice(self, delivered):
        assert send_notice("Title", "Message") is True

    def test_it_reports_false_when_notify_is_not_callable(self, monkeypatch):
        import plyer

        monkeypatch.setattr(plyer.notification, "notify", None)

        assert send_notice("Title", "Message") is False

    def test_it_reports_false_when_plyer_cannot_be_imported(self, monkeypatch):
        import sys

        # None in sys.modules is what makes an import raise
        # ImportError; it is the form tests/test_ui/
        # test_ui_action_handler.py already uses for this.
        monkeypatch.setitem(sys.modules, "plyer", None)

        assert send_notice("Title", "Message") is False

    def test_nothing_reaches_plyer_when_notify_is_not_callable(
        self, delivered, monkeypatch
    ):
        import plyer

        monkeypatch.setattr(plyer.notification, "notify", None)
        send_notice("Title", "Message")

        assert delivered == []


class TestTheSenderPassesOnlyTheArgumentsItWasGiven:
    """A caller that never named app_name keeps plyer's default."""

    def test_app_name_and_timeout_are_left_out_when_not_given(
        self, delivered
    ):
        send_notice("Title", "Message")

        assert delivered[0] == {"title": "Title", "message": "Message"}

    def test_app_name_and_timeout_are_passed_when_given(self, delivered):
        send_notice("Title", "Message", app_name="Wheelhouse", timeout=3)

        assert delivered[0] == {
            "title": "Title",
            "message": "Message",
            "app_name": "Wheelhouse",
            "timeout": 3,
        }

    def test_a_timeout_of_zero_is_still_passed(self, delivered):
        # `if timeout:` instead of `if timeout is not None:` would drop
        # it, and zero is a meaningful timeout to plyer.
        send_notice("Title", "Message", timeout=0)

        assert delivered[0]["timeout"] == 0


class TestADeliveryFailureReachesTheCaller:
    """Every caller logs its own line when the call raises."""

    def test_an_exception_from_plyer_is_not_swallowed(self, monkeypatch):
        import plyer

        def raise_it(**kwargs):
            raise RuntimeError("notification system broken")

        monkeypatch.setattr(plyer.notification, "notify", raise_it)

        with pytest.raises(RuntimeError):
            send_notice("Title", "Message")

    def test_an_overlong_notice_still_reaches_plyer(self, delivered):
        # The guard shortens; it never drops. A notice lost here is the
        # exact failure the bead was opened for.
        send_notice("t" * 400, "m" * 400)

        assert len(delivered) == 1


class TestTheSenderSaysWhyThereIsNoBackend:
    """A missing plyer must leave a line, or the silence comes back.

    This module exists because a notice can disappear with nothing in
    the log to say why. Returning False and writing nothing would put a
    smaller copy of that same silence back: six of the eleven call
    sites read the return value, and the other five ignore it, so at
    those five a broken plyer would again produce no notice and no
    record. The reason the import failed is the part that tells a
    reader whether plyer is absent or installed and broken.
    """

    def test_a_failed_plyer_import_is_logged(self, monkeypatch, caplog):
        import sys

        monkeypatch.setitem(sys.modules, "plyer", None)

        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            assert send_notice("Title", "Message") is False

        assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "a missing plyer produced no WARNING record"
        )

    def test_the_log_line_names_the_reason_the_import_failed(
        self, monkeypatch, caplog
    ):
        import sys

        monkeypatch.setitem(sys.modules, "plyer", None)

        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            send_notice("Title", "Message")

        written = " ".join(r.getMessage() for r in caplog.records)

        # "None in sys.modules" raises ModuleNotFoundError, which is a
        # subclass of ImportError; the recorded name is the subclass,
        # measured rather than assumed. The name is what separates
        # "plyer is not installed" from "plyer is installed and raises
        # on import", so the line has to carry it.
        assert "ModuleNotFoundError" in written, written
        assert "None in sys.modules" in written, written

    def test_a_notify_that_is_not_callable_is_logged_too(
        self, monkeypatch, caplog
    ):
        import plyer

        monkeypatch.setattr(plyer.notification, "notify", None)

        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            assert send_notice("Title", "Message") is False

        written = " ".join(r.getMessage() for r in caplog.records)

        assert "notify" in written, written

    def test_a_working_backend_writes_no_warning(self, delivered, caplog):
        with caplog.at_level(logging.WARNING, logger="utils.notice_text"):
            assert send_notice("Title", "Message") is True

        assert [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ] == [], "an ordinary notice must not warn"
