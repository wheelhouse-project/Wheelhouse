"""A spoken window name matches a title as whole words (wh-safety-word-free-commands, S1b).

"show <app>" runs without the safety word, so any words can reach the
window lookup: "show me" gives the target "me", which the old title
match (re.search of the target as a regular expression) found inside
"Welcome - Visual Studio Code". The match now treats the spoken phrase
as plain text, removes ending punctuation, and needs the phrase to start
and end at a word boundary in the title.

Every title match in input_proc goes through one helper,
_window_title_has_phrase: _find_window_by_target (activate, switch to,
go to, show, minimize, maximize, close <app>) and _target_is_foreground
(the activation verification).
"""
import logging
from unittest.mock import MagicMock

import pytest

import input_proc

LOGGER = logging.getLogger("test_window_title_match")


class TestTitleHelper:
    """The helper decides one thing: does the title hold the phrase."""

    @pytest.mark.parametrize(
        "phrase, title",
        [
            ("me", "Welcome - Visual Studio Code"),
            ("me.", "Welcome - Visual Studio Code"),
            ("show me", "Welcome - Visual Studio Code"),
            ("Show me.", "Welcome - Visual Studio Code"),
            ("art", "Start menu"),
            ("show art", "Start menu"),
            # The phrase ends inside the next word: only the boundary after
            # the phrase rejects these.
            ("art", "Article - Edge"),
            ("show art", "Show artwork - Paint"),
            ("details", "Detailsview - Chrome"),
        ],
    )
    def test_a_phrase_inside_a_longer_word_does_not_match(self, phrase, title):
        assert input_proc._window_title_has_phrase(title, phrase) is False

    @pytest.mark.parametrize(
        "phrase, title",
        [
            ("art", "Art class - Word"),
            ("details", "Order details - Chrome"),
            ("DETAILS", "order details - chrome"),
            ("visual studio", "Welcome - Visual Studio Code"),
            ("code", "Welcome - Visual Studio Code"),
        ],
    )
    def test_a_phrase_made_of_whole_words_matches(self, phrase, title):
        assert input_proc._window_title_has_phrase(title, phrase) is True

    def test_ending_punctuation_is_removed_from_the_phrase(self):
        assert input_proc._window_title_has_phrase(
            "Order details - Chrome", "details."
        ) is True
        assert input_proc._window_title_has_phrase(
            "Order details - Chrome", "details?!"
        ) is True

    def test_the_phrase_has_no_regular_expression_meaning(self):
        # As a regex, "a.c" would match "abc" and ".*" would match anything.
        assert input_proc._window_title_has_phrase("abc - Word", "a.c") is False
        assert input_proc._window_title_has_phrase("a.c - Word", "a.c") is True
        assert input_proc._window_title_has_phrase("Anything", ".*") is False
        # An unbalanced bracket must not raise.
        assert input_proc._window_title_has_phrase("x y", "[") is False

    def test_a_phrase_that_is_only_punctuation_matches_nothing(self):
        assert input_proc._window_title_has_phrase("Order details.", ".") is False
        assert input_proc._window_title_has_phrase("Order details", "") is False
        assert input_proc._window_title_has_phrase("Order details", "  ") is False

    def test_a_phrase_ending_in_a_symbol_matches_that_title(self):
        # Plain \b fails after "+" because "+" is not a word character;
        # the boundary is "not next to another word character".
        assert input_proc._window_title_has_phrase(
            "new 1 - Notepad++", "notepad++"
        ) is True
        assert input_proc._window_title_has_phrase(
            "new 1 - Notepad++ (admin)", "notepad++"
        ) is True
        assert input_proc._window_title_has_phrase(
            "C++ primer", "c++"
        ) is True

    def test_a_symbol_phrase_still_needs_a_boundary_before_its_first_letter(self):
        assert input_proc._window_title_has_phrase(
            "xnotepad++", "notepad++"
        ) is False


def _fake_windows(monkeypatch, windows, process_names=None):
    """Replace win32gui/win32process/psutil lookups with a fixed window list.

    windows: {hwnd: title}; process_names: {hwnd: exe name}.
    """
    import win32gui
    import win32process

    process_names = process_names or {}
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "GetWindowText", lambda hwnd: windows[hwnd])

    def enum(callback, extra):
        for hwnd in windows:
            if callback(hwnd, extra) is False:
                return

    monkeypatch.setattr(win32gui, "EnumWindows", enum)
    monkeypatch.setattr(
        win32process, "GetWindowThreadProcessId", lambda hwnd: (0, hwnd)
    )
    monkeypatch.setattr(
        input_proc.psutil, "Process",
        lambda pid: MagicMock(**{"name.return_value": process_names.get(pid, "x.exe")}),
    )


class TestFindWindowByTarget:
    def test_me_does_not_find_welcome(self, monkeypatch):
        _fake_windows(monkeypatch, {1: "Welcome - Visual Studio Code"})
        assert input_proc._find_window_by_target("me", LOGGER) is None
        assert input_proc._find_window_by_target("Me.", LOGGER) is None

    def test_art_does_not_find_start_menu_but_finds_art_class(self, monkeypatch):
        _fake_windows(monkeypatch, {1: "Start menu"})
        assert input_proc._find_window_by_target("art", LOGGER) is None
        _fake_windows(monkeypatch, {1: "Start menu", 2: "Art class - Word"})
        assert input_proc._find_window_by_target("art", LOGGER) == 2

    def test_details_finds_order_details(self, monkeypatch):
        _fake_windows(monkeypatch, {1: "Order details - Chrome"})
        assert input_proc._find_window_by_target("details", LOGGER) == 1

    def test_notepad_plus_plus_finds_its_window(self, monkeypatch):
        _fake_windows(monkeypatch, {1: "new 1 - Notepad++"})
        assert input_proc._find_window_by_target("notepad++", LOGGER) == 1

    def test_an_exe_target_still_matches_the_process_name(self, monkeypatch):
        # Protected case: "code.exe" and "notepad.exe" name a process; the
        # title is not read, and the name compares case-insensitively.
        _fake_windows(
            monkeypatch,
            {1: "Welcome - Visual Studio Code", 2: "Untitled - Notepad"},
            process_names={1: "Code.exe", 2: "notepad.exe"},
        )
        assert input_proc._find_window_by_target("code.exe", LOGGER) == 1
        assert input_proc._find_window_by_target("NOTEPAD.EXE", LOGGER) == 2
        assert input_proc._find_window_by_target("brave.exe", LOGGER) is None

    def test_an_exe_target_ignores_a_title_that_holds_its_name(self, monkeypatch):
        _fake_windows(
            monkeypatch,
            {1: "how to use code.exe - Chrome"},
            process_names={1: "chrome.exe"},
        )
        assert input_proc._find_window_by_target("code.exe", LOGGER) is None


class TestTargetIsForeground:
    """The activation verification uses the same title rule."""

    def _foreground(self, monkeypatch, title):
        import win32gui

        monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 7)
        monkeypatch.setattr(win32gui, "GetWindowText", lambda hwnd: title)

    def test_a_partial_word_is_not_the_foreground_window(self, monkeypatch):
        self._foreground(monkeypatch, "Welcome - Visual Studio Code")
        assert input_proc._target_is_foreground("me", LOGGER) is False

    def test_whole_words_are_the_foreground_window(self, monkeypatch):
        self._foreground(monkeypatch, "Order details - Chrome")
        assert input_proc._target_is_foreground("details", LOGGER) is True

    def test_an_exe_target_compares_the_process_name(self, monkeypatch):
        import win32process

        self._foreground(monkeypatch, "Anything")
        monkeypatch.setattr(
            win32process, "GetWindowThreadProcessId", lambda hwnd: (0, 7)
        )
        monkeypatch.setattr(
            input_proc.psutil, "Process",
            lambda pid: MagicMock(**{"name.return_value": "Code.exe"}),
        )
        assert input_proc._target_is_foreground("code.exe", LOGGER) is True
        assert input_proc._target_is_foreground("notepad.exe", LOGGER) is False

    def test_a_failed_process_lookup_is_not_the_foreground_window(self, monkeypatch):
        # A window that cannot be identified must read as NOT in front: the
        # verification then stops the rule and no later key goes to it.
        import win32process

        self._foreground(monkeypatch, "Anything")

        def refuse(hwnd):
            raise OSError("access denied")

        monkeypatch.setattr(win32process, "GetWindowThreadProcessId", refuse)
        assert input_proc._target_is_foreground("code.exe", LOGGER) is False

    def test_a_failed_process_name_read_is_not_the_foreground_window(self, monkeypatch):
        import win32process

        self._foreground(monkeypatch, "Anything")
        monkeypatch.setattr(
            win32process, "GetWindowThreadProcessId", lambda hwnd: (0, 7)
        )

        def gone(pid):
            raise input_proc.psutil.NoSuchProcess(pid)

        monkeypatch.setattr(input_proc.psutil, "Process", gone)
        assert input_proc._target_is_foreground("code.exe", LOGGER) is False

    def test_a_failed_foreground_read_is_not_the_foreground_window(self, monkeypatch):
        import win32gui

        def refuse():
            raise OSError("no foreground window")

        monkeypatch.setattr(win32gui, "GetForegroundWindow", refuse)
        assert input_proc._target_is_foreground("details", LOGGER) is False
        assert input_proc._target_is_foreground("code.exe", LOGGER) is False

    def test_a_failed_title_read_is_not_the_foreground_window(self, monkeypatch):
        import win32gui

        monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 7)

        def refuse(hwnd):
            raise OSError("window closed")

        monkeypatch.setattr(win32gui, "GetWindowText", refuse)
        assert input_proc._target_is_foreground("details", LOGGER) is False
