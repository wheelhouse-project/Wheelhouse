"""Tests for the activate_window launch fallback (wh-activate-launch-fallback).

"x-ray notepad" with Notepad not running matched the pattern, reached the
Input process, found no window, and silently did nothing -- activate was
activation-only. For process (.exe) targets, _handle_activate_window now
falls back to launching the executable via os.startfile (ShellExecute:
resolves System32, PATH, and the App Paths registry, which covers
notepad.exe, brave.exe, msedge.exe, and code.exe). Title-regex targets
keep the activation-only behavior, and a launch failure degrades to the
previous warning-only outcome instead of raising.
"""
import os
import threading
import time
from unittest.mock import MagicMock

import pytest

import input_proc


@pytest.fixture(autouse=True)
def _no_launches_left_over():
    """Keep one test's launches out of the next test's in-flight count.

    input_proc._launch_threads is module state that outlives a test. A
    test that leaves a blocked launch running would otherwise spend a
    slot of the next test's limit, and that test would be refused a
    launch for a reason that has nothing to do with what it checks. The
    list is cleared before the test and drained after it.
    """
    with input_proc._launch_threads_lock:
        input_proc._launch_threads.clear()
    yield
    with input_proc._launch_threads_lock:
        started = list(input_proc._launch_threads)
        input_proc._launch_threads.clear()
    # The blocking-launcher fixture releases its launches before this
    # runs, because pytest tears an autouse fixture down after the
    # fixtures the test asked for by name.
    for _, thread in started:
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _no_real_wait_for_a_started_store_window(monkeypatch):
    """Keep the wait for a started Store program's window out of real time.

    wh-activate-windows-terminal.3: after a Store program starts, the
    launch thread polls for its window so it can bring it forward. A
    test that starts a Store program and never shows it a window would
    otherwise wait the whole real bound. A bound of zero checks once and
    stops. The tests of the wait itself set their own bound and clock.
    raising=False so the file still loads against code without the wait.
    """
    monkeypatch.setattr(
        input_proc, "_STORE_WINDOW_WAIT_S", 0.0, raising=False
    )
    # Boss ruling R4: the started window is brought forward only if the
    # foreground window is the same as when the launch started. The real
    # foreground window belongs to whatever the machine running the test
    # has in front, so every test reads one fixed window instead. The
    # tests of the guard itself set their own answers.
    monkeypatch.setattr(
        input_proc, "_foreground_window", lambda: 0x5150, raising=False
    )


@pytest.fixture
def startfile_calls(monkeypatch):
    """Record os.startfile calls without launching anything.

    raising=False because os.startfile only exists on Windows; on a
    non-Windows sandbox the attribute is created for the test.
    """
    calls = []
    monkeypatch.setattr(
        os, "startfile", lambda target: calls.append(target), raising=False
    )
    return calls


def _startfile_that_fails(monkeypatch, failing):
    """Record os.startfile calls, raising OSError for the named targets.

    ``failing`` is the set of launch targets a stale shortcut stands for:
    the .lnk file is still on disk, so the lookup reports it, but
    ShellExecute cannot resolve what it points at.
    """
    calls = []

    def startfile(target):
        calls.append(target)
        if target in failing:
            raise OSError("the shortcut points at nothing")

    monkeypatch.setattr(os, "startfile", startfile, raising=False)
    return calls


def _join_launch(launch_thread, timeout=10):
    """Wait for the launch _handle_activate_window started, if it started one.

    wh-launch-off-command-loop moved both launches onto their own thread,
    so a test that reads what a launch did must wait for it. The bound
    keeps a launch that never returns from holding the suite; a launch
    still running when it runs out fails the assertion the test came for,
    which is the right outcome.
    """
    if launch_thread is not None:
        launch_thread.join(timeout=timeout)


def _handle(monkeypatch, target, *, found_hwnd, request_id=None,
            programs=(), notices=None, looked_up=None):
    """Drive _handle_activate_window with the window search stubbed out.

    The installed-program lookup is stubbed too, and defaults to finding
    nothing: no test in this file may depend on what happens to be
    installed on the machine running it. Pass ``programs`` for the
    matches the lookup should report, ``notices`` for a list to collect
    the notice text, and ``looked_up`` for a list recording the names the
    lookup was asked about.
    """
    monkeypatch.setattr(
        input_proc, "_find_window_by_target", lambda t, logger: found_hwnd
    )
    activated = []
    monkeypatch.setattr(
        input_proc,
        "_activate_window_impl",
        lambda hwnd, logger: activated.append(hwnd) or True,
    )

    def lookup(name):
        if looked_up is not None:
            looked_up.append(name)
        return list(programs)

    is_internal_action = threading.Event()
    launch_thread = input_proc._handle_activate_window(
        {"target": target},
        request_id,
        MagicMock(),
        "activate_window",
        is_internal_action,
        50,
        10,
        {},
        MagicMock(),
        notify=(lambda title, message: notices.append(message))
        if notices is not None
        else None,
        find_programs=lookup,
    )
    # wh-launch-off-command-loop: the launch runs on its own thread now,
    # so every test that reads what a launch did waits for it here. One
    # wait in this helper is what leaves all the tests below unchanged.
    _join_launch(launch_thread)
    assert not is_internal_action.is_set()
    return activated


class TestActivateWindowLaunchFallback:
    def test_exe_target_launches_when_no_window_found(
        self, monkeypatch, startfile_calls
    ):
        activated = _handle(monkeypatch, "notepad.exe", found_hwnd=None)
        assert startfile_calls == ["notepad.exe"]
        assert activated == []

    def test_exe_target_activates_existing_window_without_launching(
        self, monkeypatch, startfile_calls
    ):
        activated = _handle(monkeypatch, "notepad.exe", found_hwnd=4242)
        assert startfile_calls == []
        assert activated == [4242]

    def test_title_target_with_no_lookup_match_never_launches(
        self, monkeypatch, startfile_calls
    ):
        """Was test_title_target_never_launches. A spoken name now reaches
        the installed-program lookup (wh-activate-launch-fallback), so the
        guard this test was written for is the case where the lookup finds
        nothing: still no launch, exactly as before.
        """
        activated = _handle(monkeypatch, "Untitled - Notepad", found_hwnd=None)
        assert startfile_calls == []
        assert activated == []

    def test_launch_failure_degrades_to_warning(self, monkeypatch):
        def boom(target):
            raise OSError("no association")

        monkeypatch.setattr(os, "startfile", boom, raising=False)
        # Must not raise; the handler swallows the launch failure exactly
        # like the old no-window case.
        activated = _handle(monkeypatch, "ghost.exe", found_hwnd=None)
        assert activated == []

    def test_a_failed_exe_launch_says_which_program_would_not_start(
        self, monkeypatch
    ):
        """The .exe arm of the notice the shortcut path already shows.

        wh-exe-launch-notice, David's item 13. A spoken name whose
        lookup settles on a program and then cannot start it says so
        (test_a_launch_failure_says_which_program_would_not_start, in
        TestActivateWindowSpokenNameLookup). The .exe arm reached the
        same dead end in silence: a warning in the log, which the user
        never sees, and nothing on screen. The user asked for a program
        and it did not come, which is the defect class of
        wh-keyboard-refusal-notice.

        The whole notice list is asserted, not just its contents. The
        launch fails at once, so the slow-launch timer must not have
        fired either, and a test that only searched the list for the
        failure text would pass with a spurious slow notice beside it.
        """
        def boom(target):
            raise OSError("no association")

        monkeypatch.setattr(os, "startfile", boom, raising=False)
        notices = []
        _handle(monkeypatch, "ghost.exe", found_hwnd=None, notices=notices)
        assert notices == ["Could not start ghost.exe."]


def _program(name, target=None, fallbacks=()):
    from utils.installed_programs import USER_START_MENU, InstalledProgram

    return InstalledProgram(
        name=name,
        launch_target=target or f"C:\\{name}.lnk",
        source=USER_START_MENU,
        fallbacks=fallbacks,
    )


class TestActivateWindowSpokenNameLookup:
    """wh-activate-launch-fallback, David's items 32 and 43.

    "x-ray activate outlook" is a title pattern, so before this there was
    nothing to run when Outlook was closed and the command did nothing at
    all. A spoken name that matches no window is now looked up among the
    installed programs: one match starts, several start nothing and are
    listed, none says so.
    """

    def test_one_match_starts_that_program(self, monkeypatch, startfile_calls):
        notices = []
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
            notices=notices,
        )
        assert startfile_calls == ["C:\\Start\\Outlook.lnk"]

    def test_one_match_says_what_it_is_starting(self, monkeypatch, startfile_calls):
        notices = []
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook")],
            notices=notices,
        )
        assert notices == ["Starting Outlook"]

    def test_several_matches_start_nothing(self, monkeypatch, startfile_calls):
        notices = []
        _handle(
            monkeypatch,
            "note",
            found_hwnd=None,
            programs=[_program("Notepad"), _program("Notepad++")],
            notices=notices,
        )
        assert startfile_calls == []

    def test_several_matches_list_the_names(self, monkeypatch, startfile_calls):
        notices = []
        _handle(
            monkeypatch,
            "note",
            found_hwnd=None,
            programs=[_program("Notepad"), _program("Notepad++")],
            notices=notices,
        )
        assert len(notices) == 1
        assert "Notepad" in notices[0]
        assert "Notepad++" in notices[0]

    def test_a_long_list_of_names_is_capped(self, monkeypatch, startfile_calls):
        """A Windows toast truncates long text silently, so the notice caps
        the names itself and says how many it left out.
        """
        notices = []
        _handle(
            monkeypatch,
            "a",
            found_hwnd=None,
            programs=[_program(f"App{n}") for n in range(1, 8)],
            notices=notices,
        )
        assert len(notices) == 1
        assert "App5" in notices[0]
        assert "App6" not in notices[0]
        assert "2 more" in notices[0]

    def test_the_notice_never_lists_one_name_twice(
        self, monkeypatch, startfile_calls
    ):
        """Boss ruling 2026-09-04, from wh-activate-launch-fallback.1.1.

        The notice exists so the user can pick by saying the full name,
        so a name listed twice offers no choice and leaves no way out.
        This runs the real lookup over candidates shaped like the ones
        measured on the developer's machine -- one program reported by
        both Start Menu scopes and by App Paths -- and reads the notice
        the handler builds from the result.

        The one shape this cannot promise is two subfolders of ONE Start
        Menu scope holding the same shortcut name. Those stay ambiguous
        under the same ruling, because they can be two different
        programs, and the notice then repeats the name.
        """
        from utils.installed_programs import (
            APP_PATHS,
            COMMON_START_MENU,
            USER_START_MENU,
            InstalledProgram,
            find_installed_programs,
        )

        candidates = [
            InstalledProgram("Teams", "C:\\Users\\me\\Teams.lnk", USER_START_MENU),
            InstalledProgram(
                "Teams", "C:\\ProgramData\\Teams.lnk", COMMON_START_MENU
            ),
            InstalledProgram("teams", "teams.exe", APP_PATHS),
            InstalledProgram(
                "Teams Classic",
                "C:\\ProgramData\\Teams Classic.lnk",
                COMMON_START_MENU,
            ),
        ]
        programs = find_installed_programs("team", scan=lambda: candidates)

        notices = []
        _handle(
            monkeypatch,
            "team",
            found_hwnd=None,
            programs=programs,
            notices=notices,
        )

        assert startfile_calls == []
        assert len(notices) == 1
        listed = notices[0].split(": ", 1)[1].split(", ")
        assert listed == ["Teams", "Teams Classic"]
        assert len(listed) == len(set(listed))

    def test_no_match_starts_nothing_and_says_so(
        self, monkeypatch, startfile_calls
    ):
        notices = []
        _handle(
            monkeypatch,
            "hyperion",
            found_hwnd=None,
            programs=[],
            notices=notices,
        )
        assert startfile_calls == []
        assert len(notices) == 1
        assert "hyperion" in notices[0]

    def test_an_exe_target_never_reaches_the_lookup(
        self, monkeypatch, startfile_calls
    ):
        """An .exe keeps the direct-launch path (criterion 5)."""
        looked_up = []
        _handle(
            monkeypatch,
            "notepad.exe",
            found_hwnd=None,
            looked_up=looked_up,
        )
        assert looked_up == []
        assert startfile_calls == ["notepad.exe"]

    def test_a_found_window_never_reaches_the_lookup(
        self, monkeypatch, startfile_calls
    ):
        looked_up = []
        activated = _handle(
            monkeypatch, "outlook", found_hwnd=4242, looked_up=looked_up
        )
        assert looked_up == []
        assert activated == [4242]
        assert startfile_calls == []

    def test_the_spoken_words_are_what_gets_looked_up(self, monkeypatch):
        looked_up = []
        _handle(monkeypatch, "outlook", found_hwnd=None, looked_up=looked_up)
        assert looked_up == ["outlook"]

    def test_a_launch_failure_does_not_raise(self, monkeypatch):
        def boom(target):
            raise OSError("no association")

        monkeypatch.setattr(os, "startfile", boom, raising=False)
        notices = []
        # Must not raise: the same degradation the .exe path already has.
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook")],
            notices=notices,
        )

    def test_a_launch_failure_says_which_program_would_not_start(
        self, monkeypatch
    ):
        """The user asked for a program. Failing in silence is the defect
        class of wh-keyboard-refusal-notice, so the failure is spoken.
        """
        def boom(target):
            raise OSError("no association")

        monkeypatch.setattr(os, "startfile", boom, raising=False)
        notices = []
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook")],
            notices=notices,
        )
        assert len(notices) == 1
        assert "Outlook" in notices[0]
        assert "Starting" not in notices[0]

    def test_no_notifier_still_starts_the_program(
        self, monkeypatch, startfile_calls
    ):
        """A caller that passes no notifier (the legacy signature) must
        still launch; the notice is the only thing it gives up.
        """
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert startfile_calls == ["C:\\Start\\Outlook.lnk"]


def _valid_buffer():
    """Build a real ShadowBufferManager that reports is_valid True."""
    from ui.shadow_buffer import ShadowBufferManager

    buffer = ShadowBufferManager()
    buffer.update_from_clipboard_data("hello", 5, 0)
    assert buffer.is_valid is True
    return buffer


def _handle_with_buffer(monkeypatch, target, *, found_hwnd, buffer, programs=()):
    """Drive _handle_activate_window with a shadow buffer wired in.

    The lookup is stubbed here for the same reason as in ``_handle``: no
    test may depend on what is installed on the machine running it.
    """
    monkeypatch.setattr(
        input_proc, "_find_window_by_target", lambda t, logger: found_hwnd
    )
    monkeypatch.setattr(
        input_proc, "_activate_window_impl", lambda hwnd, logger: True
    )
    launch_thread = input_proc._handle_activate_window(
        {"target": target},
        None,
        MagicMock(),
        "activate_window",
        threading.Event(),
        50,
        10,
        {},
        MagicMock(),
        buffer_manager=buffer,
        find_programs=lambda name: list(programs),
    )
    # wh-launch-off-command-loop: the launch, and the buffer invalidation
    # that follows a successful one, run on the launch thread.
    _join_launch(launch_thread)


class TestActivateWindowShadowBufferInvalidation:
    """wh-review-pattern-fixes.8: activate_window bypasses UIActionHandler,
    but a successful voice activate changes foreground focus. The input
    listeners suppress invalidation during internal actions, so the
    handler must invalidate the shadow buffer itself, before the
    activation dispatch. A pure no-op (no window found, nothing launched)
    keeps the buffer valid.
    """

    def test_successful_activation_invalidates_buffer(self, monkeypatch):
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch, "Untitled - Notepad", found_hwnd=4242, buffer=buffer
        )
        assert buffer.is_valid is False

    def test_launch_fallback_invalidates_buffer(
        self, monkeypatch, startfile_calls
    ):
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch, "notepad.exe", found_hwnd=None, buffer=buffer
        )
        assert startfile_calls == ["notepad.exe"]
        assert buffer.is_valid is False

    def test_no_window_no_launch_keeps_buffer_valid(
        self, monkeypatch, startfile_calls
    ):
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch, "Untitled - Notepad", found_hwnd=None, buffer=buffer
        )
        assert startfile_calls == []
        assert buffer.is_valid is True

    def test_spoken_name_launch_invalidates_buffer(
        self, monkeypatch, startfile_calls
    ):
        """The looked-up program takes focus just as a .exe launch does."""
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            buffer=buffer,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert startfile_calls == ["C:\\Start\\Outlook.lnk"]
        assert buffer.is_valid is False

    def test_a_launch_that_failed_keeps_the_buffer_valid(self, monkeypatch):
        """wh-activate-launch-fallback.1.7: nothing started, nothing moved.

        The buffer was invalidated before os.startfile, and the OSError
        branch returned without undoing it, so a dangling shortcut cost
        the user the buffer for a program that never appeared.
        ShadowBufferManager has no undo -- invalidate() clears the text,
        the cursor and the selection, and the only way back is a fresh
        UIA synchronize -- so the invalidation has to wait for the launch
        to succeed.
        """
        def boom(target):
            raise OSError("the shortcut points at nothing")

        monkeypatch.setattr(os, "startfile", boom, raising=False)
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            buffer=buffer,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert buffer.is_valid is True

    def test_a_fallback_that_starts_still_invalidates_the_buffer(
        self, monkeypatch
    ):
        """The program that did start takes focus like any other."""
        calls = _startfile_that_fails(monkeypatch, {"C:\\Start\\Outlook.lnk"})
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            buffer=buffer,
            programs=[
                _program(
                    "Outlook",
                    "C:\\Start\\Outlook.lnk",
                    fallbacks=(_program("outlook", "outlook.exe"),),
                )
            ],
        )
        assert calls == ["C:\\Start\\Outlook.lnk", "outlook.exe"]
        assert buffer.is_valid is False

    def test_several_matches_keep_buffer_valid(
        self, monkeypatch, startfile_calls
    ):
        """Nothing starts, so nothing takes focus."""
        buffer = _valid_buffer()
        _handle_with_buffer(
            monkeypatch,
            "note",
            found_hwnd=None,
            buffer=buffer,
            programs=[_program("Notepad"), _program("Notepad++")],
        )
        assert startfile_calls == []
        assert buffer.is_valid is True


class TestActivateWindowFailedLaunchFallback:
    """wh-activate-launch-fallback.1.6, ruled by the boss 2026-09-04.

    A name is claimed for the first source that reports it, and nothing
    checks that its shortcut resolves. A stale user Start Menu shortcut
    with a good machine-wide install behind it therefore used to start
    nothing at all, and the notice never named the entry that would have
    worked, so the user had no second thing to say. The winner is still
    the program named and started first; the entries that yielded to it
    are tried, in source order, only when os.startfile raises.
    """

    def test_a_stale_winner_falls_back_to_the_next_source(self, monkeypatch):
        calls = _startfile_that_fails(monkeypatch, {"C:\\Start\\Excel.lnk"})
        notices = []
        _handle(
            monkeypatch,
            "excel",
            found_hwnd=None,
            programs=[
                _program(
                    "Excel",
                    "C:\\Start\\Excel.lnk",
                    fallbacks=(_program("excel", "excel.exe"),),
                )
            ],
            notices=notices,
        )
        assert calls == ["C:\\Start\\Excel.lnk", "excel.exe"]
        assert notices == ["Starting Excel"]

    def test_the_fallbacks_are_tried_in_the_order_they_are_given(
        self, monkeypatch
    ):
        calls = _startfile_that_fails(
            monkeypatch,
            {"C:\\Users\\me\\Chrome.lnk", "C:\\ProgramData\\Chrome.lnk"},
        )
        notices = []
        _handle(
            monkeypatch,
            "chrome",
            found_hwnd=None,
            programs=[
                _program(
                    "Chrome",
                    "C:\\Users\\me\\Chrome.lnk",
                    fallbacks=(
                        _program("Chrome", "C:\\ProgramData\\Chrome.lnk"),
                        _program("chrome", "chrome.exe"),
                    ),
                )
            ],
            notices=notices,
        )
        assert calls == [
            "C:\\Users\\me\\Chrome.lnk",
            "C:\\ProgramData\\Chrome.lnk",
            "chrome.exe",
        ]
        assert notices == ["Starting Chrome"]

    def test_every_candidate_failing_says_so_once(self, monkeypatch):
        calls = _startfile_that_fails(
            monkeypatch, {"C:\\Start\\Excel.lnk", "excel.exe"}
        )
        notices = []
        _handle(
            monkeypatch,
            "excel",
            found_hwnd=None,
            programs=[
                _program(
                    "Excel",
                    "C:\\Start\\Excel.lnk",
                    fallbacks=(_program("excel", "excel.exe"),),
                )
            ],
            notices=notices,
        )
        assert calls == ["C:\\Start\\Excel.lnk", "excel.exe"]
        assert notices == ["Could not start Excel."]

    def test_a_winner_that_starts_never_reaches_its_fallbacks(
        self, monkeypatch, startfile_calls
    ):
        """Only OSError falls through. A launch that worked is the answer."""
        notices = []
        _handle(
            monkeypatch,
            "excel",
            found_hwnd=None,
            programs=[
                _program(
                    "Excel",
                    "C:\\Start\\Excel.lnk",
                    fallbacks=(_program("excel", "excel.exe"),),
                )
            ],
            notices=notices,
        )
        assert startfile_calls == ["C:\\Start\\Excel.lnk"]
        assert notices == ["Starting Excel"]

    def test_several_matches_never_reach_a_launch_at_all(
        self, monkeypatch, startfile_calls
    ):
        """The same-scope ambiguity David ruled on is untouched by this.

        Two shortcuts of one name in one scope can be two different
        programs, so the notice asks for the full name; no candidate of
        either is tried.
        """
        notices = []
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[
                _program("Outlook", "C:\\Office\\Outlook.lnk"),
                _program("Outlook", "C:\\Other\\Outlook.lnk"),
            ],
            notices=notices,
        )
        assert startfile_calls == []
        assert notices == [
            "More than one program matches. Say the full name: Outlook, Outlook"
        ]


class _BlockingLauncher:
    """An os.startfile stand-in that waits until the test releases it.

    ``entered`` is set as soon as a launch call begins and ``left`` only
    when one returns. A test can therefore tell "the launch is under way"
    from "the launch has finished" without measuring elapsed time, which
    is what makes these tests a decision rather than a stopwatch reading.

    The wait carries a bound so that a run against code which still
    launches on the command loop ends instead of holding the suite.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.left = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def __call__(self, target):
        self.calls.append(target)
        self.entered.set()
        self.release.wait(timeout=10)
        self.left.set()


@pytest.fixture
def blocking_launcher(monkeypatch):
    """Replace os.startfile with a launch that never returns on its own."""
    launcher = _BlockingLauncher()
    monkeypatch.setattr(os, "startfile", launcher, raising=False)
    yield launcher
    launcher.release.set()


def _wait_until(predicate, timeout=5.0):
    """Poll ``predicate`` until it holds, or the bound runs out."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _drive(monkeypatch, target, *, found_hwnd=None, programs=(),
           notices=None, buffer=None):
    """Call _handle_activate_window without waiting for any launch.

    The tests below are about what the command loop can do WHILE a launch
    is still running, so they cannot use ``_handle`` or
    ``_handle_with_buffer``: both of those wait for the launch to finish.
    """
    monkeypatch.setattr(
        input_proc, "_find_window_by_target", lambda t, logger: found_hwnd
    )
    activated = []
    monkeypatch.setattr(
        input_proc,
        "_activate_window_impl",
        lambda hwnd, logger: activated.append(hwnd) or True,
    )
    input_proc._handle_activate_window(
        {"target": target},
        None,
        MagicMock(),
        "activate_window",
        threading.Event(),
        50,
        10,
        {},
        MagicMock(),
        buffer_manager=buffer,
        notify=(lambda title, message: notices.append(message))
        if notices is not None
        else None,
        find_programs=lambda name: list(programs),
    )
    return activated


class TestLaunchRunsOffTheCommandLoop:
    """wh-launch-off-command-loop, David's item 18 of QUESTIONS-2026-09-04.md.

    The Input process runs ONE synchronous command loop. Both launch
    paths used to call os.startfile on that loop, so a shortcut with a
    dead network target, or a slow shell extension, held every later
    voice command for as long as the shell call took, with no hands-free
    way out. _DispatchWatch reports such a stall and cannot end it.

    Both launches now run on their own short-lived thread. These tests
    prove the command loop is free while a launch is still running.
    """

    def test_an_exe_launch_that_blocks_does_not_hold_the_handler(
        self, monkeypatch, blocking_launcher
    ):
        _drive(monkeypatch, "notepad.exe")
        assert blocking_launcher.entered.wait(timeout=5)
        assert blocking_launcher.left.is_set() is False

    def test_a_spoken_name_launch_that_blocks_does_not_hold_the_handler(
        self, monkeypatch, blocking_launcher
    ):
        _drive(
            monkeypatch,
            "outlook",
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert blocking_launcher.entered.wait(timeout=5)
        assert blocking_launcher.left.is_set() is False

    def test_a_later_command_runs_while_an_exe_launch_is_blocked(
        self, monkeypatch, blocking_launcher
    ):
        _drive(monkeypatch, "notepad.exe")
        assert blocking_launcher.entered.wait(timeout=5)
        activated = _drive(monkeypatch, "Untitled - Notepad", found_hwnd=4242)
        assert activated == [4242]
        assert blocking_launcher.left.is_set() is False

    def test_a_later_command_runs_while_a_spoken_name_launch_is_blocked(
        self, monkeypatch, blocking_launcher
    ):
        _drive(
            monkeypatch,
            "outlook",
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert blocking_launcher.entered.wait(timeout=5)
        activated = _drive(monkeypatch, "Untitled - Notepad", found_hwnd=4242)
        assert activated == [4242]
        assert blocking_launcher.left.is_set() is False

    def test_the_installed_program_scan_also_runs_off_the_loop(
        self, monkeypatch
    ):
        """The scan reads the Start Menu folders and the registry on every
        lookup with no cache, and %APPDATA% is a network path on a roaming
        profile, so it carries the same exposure as the launch itself.
        """
        in_scan = threading.Event()
        left_scan = threading.Event()
        release = threading.Event()

        def slow_lookup(name):
            in_scan.set()
            release.wait(timeout=10)
            left_scan.set()
            return []

        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: None
        )
        monkeypatch.setattr(
            input_proc, "_activate_window_impl", lambda hwnd, logger: True
        )
        try:
            input_proc._handle_activate_window(
                {"target": "outlook"},
                None,
                MagicMock(),
                "activate_window",
                threading.Event(),
                50,
                10,
                {},
                MagicMock(),
                find_programs=slow_lookup,
            )
            assert in_scan.wait(timeout=5)
            assert left_scan.is_set() is False
        finally:
            release.set()


class TestLaunchesInFlightCap:
    """wh-launch-off-command-loop, boss ruling 2026-09-04.

    Windows offers no way to interrupt a shell call that never returns,
    so every attempt at a dead shortcut leaves one thread running for the
    life of the process. Without a limit, a user who repeats the command
    ten times leaves ten of them. The limit is checked on the command
    loop, before a thread is started, and it counts only the launches
    still running.
    """

    def test_more_launches_than_the_limit_start_nothing_and_say_so(
        self, monkeypatch, blocking_launcher
    ):
        limit = input_proc._MAX_LAUNCHES_IN_FLIGHT
        for _ in range(limit):
            _drive(monkeypatch, "notepad.exe")
        assert _wait_until(lambda: len(blocking_launcher.calls) == limit)

        notices = []
        _drive(monkeypatch, "notepad.exe", notices=notices)

        assert len(blocking_launcher.calls) == limit
        assert notices == [input_proc._LAUNCH_BUSY_NOTICE]

    def test_a_launch_that_finished_frees_a_slot(
        self, monkeypatch, startfile_calls
    ):
        limit = input_proc._MAX_LAUNCHES_IN_FLIGHT
        notices = []
        for _ in range(limit + 2):
            _handle(monkeypatch, "notepad.exe", found_hwnd=None,
                    notices=notices)
        assert len(startfile_calls) == limit + 2
        assert notices == []

    def test_a_refused_launch_keeps_the_buffer_valid(
        self, monkeypatch, blocking_launcher
    ):
        """The limit is checked before the shadow buffer is invalidated.

        Invalidating costs the next buffer query a full UIA re-sync of
        the window. A launch the limit refuses starts nothing and
        changes no focus, so it must not cost that (boss ruling
        2026-09-04 on wh-launch-off-command-loop).
        """
        limit = input_proc._MAX_LAUNCHES_IN_FLIGHT
        for _ in range(limit):
            _drive(monkeypatch, "notepad.exe")
        assert _wait_until(lambda: len(blocking_launcher.calls) == limit)

        buffer = _valid_buffer()
        notices = []
        _drive(monkeypatch, "notepad.exe", buffer=buffer, notices=notices)

        # The refusal is asserted first, so a run where the limit did
        # not refuse fails on that rather than on the buffer.
        assert notices == [input_proc._LAUNCH_BUSY_NOTICE]
        assert len(blocking_launcher.calls) == limit
        assert buffer.is_valid is True


class TestSlowLaunchNotice:
    """wh-launch-off-command-loop, boss ruling 2026-09-04.

    "Starting <name>" is shown only after the shell call returns
    (wh-activate-launch-fallback.1.7 fixed that order, and it is
    unchanged here). With the launch off the command loop, a shell call
    that never returns would therefore say nothing at all, so a launch
    that has not returned in time says so once.
    """

    def test_a_blocked_exe_launch_says_it_is_taking_a_long_time(
        self, monkeypatch, blocking_launcher
    ):
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        notices = []
        _drive(monkeypatch, "notepad.exe", notices=notices)
        assert _wait_until(
            lambda: notices == ["notepad.exe is taking a long time to start."]
        )

    def test_a_blocked_spoken_name_launch_names_the_program(
        self, monkeypatch, blocking_launcher
    ):
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        notices = []
        _drive(
            monkeypatch,
            "outlook",
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
            notices=notices,
        )
        assert _wait_until(
            lambda: notices == ["Outlook is taking a long time to start."]
        )

    def test_a_launch_that_returns_never_says_it_is_taking_a_long_time(
        self, monkeypatch, startfile_calls
    ):
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        notices = []
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
            notices=notices,
        )
        time.sleep(0.15)
        assert notices == ["Starting Outlook"]

    def test_a_launch_where_nothing_starts_never_says_it_is_slow(
        self, monkeypatch
    ):
        """Every candidate failing never reaches the cancel on the
        success path, so only the one covering the whole attempt stops
        "Could not start Outlook." being followed by a slow notice.
        """
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        _startfile_that_fails(monkeypatch, {"C:\\Start\\Outlook.lnk"})
        notices = []
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
            notices=notices,
        )
        time.sleep(0.15)
        assert notices == ["Could not start Outlook."]

    def test_a_slow_notice_cannot_make_a_finished_launch_look_slow(
        self, monkeypatch, startfile_calls
    ):
        """The timer is cancelled the moment the shell call returns, not
        only when the whole attempt is over. The notice between the two
        reaches a Windows toast and can take longer than the limit, and a
        launch that worked must not then be called slow.
        """
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        notices = []
        held = threading.Event()

        def notify(title, message):
            notices.append(message)
            if not held.is_set():
                held.set()
                time.sleep(0.3)

        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: None
        )
        monkeypatch.setattr(
            input_proc, "_activate_window_impl", lambda hwnd, logger: True
        )
        launch_thread = input_proc._handle_activate_window(
            {"target": "outlook"},
            None,
            MagicMock(),
            "activate_window",
            threading.Event(),
            50,
            10,
            {},
            MagicMock(),
            notify=notify,
            find_programs=lambda name: [
                _program("Outlook", "C:\\Start\\Outlook.lnk")
            ],
        )
        _join_launch(launch_thread)
        assert notices == ["Starting Outlook"]

    def test_a_blocked_lookup_says_the_spoken_words_are_slow(
        self, monkeypatch
    ):
        """The timer covers the lookup, not only the shell call.

        The installed-program lookup walks the Start Menu folders and
        reads the registry on every call, and %APPDATA% is a network
        path on a roaming profile, so a lookup that blocks is one of the
        things this path was moved off the command loop for. Nothing has
        resolved a program name while it blocks, so the notice says the
        words the user spoke. Filed by codex as
        wh-launch-off-command-loop.2.1.
        """
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: None
        )
        monkeypatch.setattr(
            input_proc, "_activate_window_impl", lambda hwnd, logger: True
        )
        release = threading.Event()
        notices = []

        def lookup(name):
            # The bound keeps a lookup nothing releases from holding the
            # suite; the assertion below fails first if that happens.
            release.wait(10)
            return []

        launch_thread = input_proc._handle_activate_window(
            {"target": "outlook"},
            None,
            MagicMock(),
            "activate_window",
            threading.Event(),
            50,
            10,
            {},
            MagicMock(),
            notify=lambda title, message: notices.append(message),
            find_programs=lookup,
        )
        try:
            assert _wait_until(
                lambda: notices == ["outlook is taking a long time to start."]
            )
        finally:
            release.set()
        _join_launch(launch_thread)

    def test_a_finished_exe_launch_never_says_it_is_slow(
        self, monkeypatch, startfile_calls
    ):
        """The .exe path cancels its timer in a finally.

        That path sends no notice of its own -- the one-line notice for
        it is wh-exe-launch-notice, a separate bead -- so a cancel that
        never ran is the only thing that can put text in this list. The
        finally covers the launch that worked and the launch that raised
        alike; this test is the first arm and
        test_a_failed_exe_launch_never_says_it_is_slow is the second.
        Filed by deepseek as wh-launch-off-command-loop.1.1: the
        spoken-name path's two cancels each had a mutation and a test,
        and this one had neither.
        """
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        notices = []
        _handle(monkeypatch, "notepad.exe", found_hwnd=None, notices=notices)
        # The launch is asserted first, so a run where nothing started
        # fails on that rather than on an empty notices list it would
        # have had anyway.
        assert startfile_calls == ["notepad.exe"]
        time.sleep(0.15)
        assert notices == []

    def test_a_failed_exe_launch_never_says_it_is_slow(self, monkeypatch):
        """The other arm of the same finally: os.startfile raised.

        The failure is spoken since wh-exe-launch-notice, so the list is
        no longer empty. The exact list is asserted rather than the
        absence of the slow wording: it proves the slow timer stayed
        silent AND that nothing else was said, which is what the
        emptiness used to prove. Before that bead this read
        `assert notices == []`, which also pinned the silence the bead
        removed -- the test could not tell the two apart.
        """
        monkeypatch.setattr(input_proc, "_LAUNCH_SLOW_S", 0.05)
        calls = _startfile_that_fails(monkeypatch, {"notepad.exe"})
        notices = []
        _handle(monkeypatch, "notepad.exe", found_hwnd=None, notices=notices)
        assert calls == ["notepad.exe"]
        time.sleep(0.15)
        assert notices == ["Could not start notepad.exe."]


class TestLaunchWorkerFailureIsLogged:
    """wh-launch-off-command-loop.1.1, filed by deepseek.

    The launch runs on its own thread with nothing above it to catch
    anything, so _launch_off_command_loop wraps the worker body. Without
    that wrapper an unexpected exception ends in a bare thread traceback
    on stderr and nothing reaches the process log. The spoken-name
    lookup runs entirely on the worker thread now, so a lookup that
    raises is the ordinary way in.
    """

    # Without the containment, the exception escapes the thread and
    # pytest's threadexception plugin fails the test on
    # PytestUnhandledThreadExceptionWarning before this test's own
    # assertion is reached. The mutation-gate skill calls that a false
    # catch: the mutation would be reported caught for a reason that
    # says nothing about the containment. Ignoring the warning here
    # makes the missing logger.error call the thing that fails. The
    # unmutated code raises no such warning, so this changes nothing
    # about what the test proves.
    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    def test_a_worker_that_raises_is_logged_and_ends_only_its_thread(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: None
        )
        monkeypatch.setattr(
            input_proc, "_activate_window_impl", lambda hwnd, logger: True
        )
        logger = MagicMock()

        def lookup(name):
            raise RuntimeError("the installed-programs scan blew up")

        launch_thread = input_proc._handle_activate_window(
            {"target": "outlook"},
            None,
            MagicMock(),
            "activate_window",
            threading.Event(),
            50,
            10,
            {},
            logger,
            find_programs=lookup,
        )
        assert launch_thread is not None
        _join_launch(launch_thread)
        assert not launch_thread.is_alive()
        assert logger.error.call_count == 1
        assert "Starting outlook failed" in logger.error.call_args[0][0]


# ---------------------------------------------------------------------------
# wh-activate-windows-terminal: Microsoft Store programs
# ---------------------------------------------------------------------------

_TERMINAL_ID = "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"
_TERMINAL_TARGET = "shell:AppsFolder\\" + _TERMINAL_ID


def _store_program(name, app_id, fallbacks=()):
    from utils.installed_programs import STORE_APPS, InstalledProgram

    return InstalledProgram(
        name=name,
        launch_target="shell:AppsFolder\\" + app_id,
        source=STORE_APPS,
        app_id=app_id,
        fallbacks=fallbacks,
    )


def _terminal_lookup(spoken):
    """What the real lookup returns for Windows Terminal, through its seam."""
    from utils.installed_programs import (
        _store_programs_from_records,
        find_installed_programs,
    )

    entries = _store_programs_from_records(
        [(_TERMINAL_ID, "Terminal", "Windows Terminal", 1)]
    )
    return find_installed_programs(spoken, scan=lambda: list(entries))


def _running_windows(monkeypatch, windows):
    """Make _find_window_by_app_id answer from ``windows``: {app_id: hwnd}.

    Returns the list of app IDs it was asked about, in order.
    """
    asked = []

    def find(app_id, logger):
        asked.append(app_id)
        return windows.get(app_id)

    monkeypatch.setattr(input_proc, "_find_window_by_app_id", find)
    return asked


class TestActivateWindowStoreProgram:
    """wh-activate-windows-terminal.

    Windows Terminal's window title is the active tab's title, so the title
    search finds nothing, and starting it while it runs opens a second
    window. A Store program the lookup found is brought forward when it
    already has a window, and started only when it has none.
    """

    def test_a_running_store_program_is_brought_forward_not_started(
        self, monkeypatch, startfile_calls
    ):
        notices = []
        asked = _running_windows(monkeypatch, {_TERMINAL_ID: 777})
        activated = _handle(
            monkeypatch,
            "terminal",
            found_hwnd=None,
            programs=_terminal_lookup("terminal"),
            notices=notices,
        )
        assert activated == [777]
        assert startfile_calls == []
        assert notices == []
        assert asked == [_TERMINAL_ID]

    def test_the_package_name_of_a_running_store_program_brings_it_forward(
        self, monkeypatch, startfile_calls
    ):
        # "windows terminal" matches the package-name entry, not the tile
        # name "Terminal"; it must still find the running window.
        notices = []
        asked = _running_windows(monkeypatch, {_TERMINAL_ID: 777})
        activated = _handle(
            monkeypatch,
            "windows terminal",
            found_hwnd=None,
            programs=_terminal_lookup("windows terminal"),
            notices=notices,
        )
        assert activated == [777]
        assert startfile_calls == []
        assert notices == []
        assert asked == [_TERMINAL_ID]

    def test_bringing_a_running_program_forward_invalidates_the_buffer(
        self, monkeypatch, startfile_calls
    ):
        buffer = _valid_buffer()
        _running_windows(monkeypatch, {_TERMINAL_ID: 777})
        _handle_with_buffer(
            monkeypatch,
            "terminal",
            found_hwnd=None,
            buffer=buffer,
            programs=_terminal_lookup("terminal"),
        )
        assert startfile_calls == []
        assert buffer.is_valid is False

    def test_a_fallback_entry_with_a_running_window_is_brought_forward(
        self, monkeypatch, startfile_calls
    ):
        winner = _program(
            "Terminal",
            "C:\\Start\\Terminal.lnk",
            fallbacks=(_store_program("Terminal", _TERMINAL_ID),),
        )
        _running_windows(monkeypatch, {_TERMINAL_ID: 555})
        activated = _handle(
            monkeypatch, "terminal", found_hwnd=None, programs=[winner]
        )
        assert activated == [555]
        assert startfile_calls == []

    def test_a_store_program_with_no_window_is_started(
        self, monkeypatch, startfile_calls
    ):
        notices = []
        _running_windows(monkeypatch, {})
        activated = _handle(
            monkeypatch,
            "terminal",
            found_hwnd=None,
            programs=_terminal_lookup("terminal"),
            notices=notices,
        )
        assert activated == []
        assert startfile_calls == [_TERMINAL_TARGET]
        assert notices == ["Starting Terminal"]

    def test_the_start_uses_the_found_entry_never_the_spoken_words(
        self, monkeypatch, startfile_calls
    ):
        _running_windows(monkeypatch, {})
        _handle(
            monkeypatch,
            "windows terminal",
            found_hwnd=None,
            programs=_terminal_lookup("windows terminal"),
        )
        assert startfile_calls == [_TERMINAL_TARGET]
        assert "windows terminal" not in startfile_calls[0].casefold()

    def test_a_program_with_no_app_id_never_searches_by_app_id(
        self, monkeypatch, startfile_calls
    ):
        asked = _running_windows(monkeypatch, {})
        _handle(
            monkeypatch,
            "outlook",
            found_hwnd=None,
            programs=[_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert asked == []
        assert startfile_calls == ["C:\\Start\\Outlook.lnk"]

    def test_no_match_says_so_and_starts_nothing(self, monkeypatch, startfile_calls):
        notices = []
        asked = _running_windows(monkeypatch, {})
        _handle(
            monkeypatch,
            "hyperion",
            found_hwnd=None,
            programs=_terminal_lookup("hyperion"),
            notices=notices,
        )
        assert startfile_calls == []
        assert asked == []
        assert notices == ["No program matched hyperion."]

    def test_several_store_matches_start_none_and_list_them(
        self, monkeypatch, startfile_calls
    ):
        notices = []
        asked = _running_windows(monkeypatch, {"Vendor.A_1!App": 9})
        _handle(
            monkeypatch,
            "notes",
            found_hwnd=None,
            programs=[
                _store_program("Notes", "Vendor.A_1!App"),
                _store_program("Notes", "Vendor.B_2!App"),
            ],
            notices=notices,
        )
        assert startfile_calls == []
        assert asked == []
        assert len(notices) == 1
        assert notices[0].startswith("More than one program matches.")
        assert notices[0].count("Notes") == 2


class TestFindWindowByAppId:
    """_find_window_by_app_id, with the window and process readers stubbed."""

    def _windows(self, monkeypatch, windows, process_ids, window_ids):
        """windows: [(hwnd, visible, title, pid)]; the two dicts map to app IDs."""
        def enum(callback, extra):
            for hwnd, _visible, _title, _pid in windows:
                if callback(hwnd, extra) is False:
                    return

        by_hwnd = {w[0]: w for w in windows}
        monkeypatch.setattr(input_proc.win32gui, "EnumWindows", enum)
        monkeypatch.setattr(
            input_proc.win32gui, "IsWindowVisible", lambda h: by_hwnd[h][1]
        )
        monkeypatch.setattr(
            input_proc.win32gui, "GetWindowText", lambda h: by_hwnd[h][2]
        )
        monkeypatch.setattr(
            input_proc.win32process,
            "GetWindowThreadProcessId",
            lambda h: (0, by_hwnd[h][3]),
        )
        monkeypatch.setattr(input_proc, "_process_app_id", lambda pid: process_ids.get(pid))
        monkeypatch.setattr(input_proc, "_window_app_id", lambda hwnd: window_ids.get(hwnd))

    def test_a_process_with_that_app_id_matches_ignoring_case(self, monkeypatch):
        self._windows(
            monkeypatch,
            [(1, True, "Other", 10), (2, True, "PowerShell", 20)],
            process_ids={10: "Vendor.Other_1!App", 20: _TERMINAL_ID.upper()},
            window_ids={},
        )
        assert input_proc._find_window_by_app_id(_TERMINAL_ID, MagicMock()) == 2

    def test_a_window_property_with_that_app_id_matches(self, monkeypatch):
        """A UWP window hosted by ApplicationFrameHost: the process is the host."""
        self._windows(
            monkeypatch,
            [(1, True, "Settings", 10)],
            process_ids={},
            window_ids={1: "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel"},
        )
        found = input_proc._find_window_by_app_id(
            "Windows.ImmersiveControlPanel_cw5n1h2txyewy!Microsoft.Windows.ImmersiveControlPanel",
            MagicMock(),
        )
        assert found == 1

    def test_a_desktop_process_never_matches(self, monkeypatch):
        self._windows(
            monkeypatch,
            [(1, True, "PowerShell", 10)],
            process_ids={10: None},
            window_ids={1: None},
        )
        assert input_proc._find_window_by_app_id(_TERMINAL_ID, MagicMock()) is None

    def test_invisible_and_untitled_windows_are_skipped(self, monkeypatch):
        self._windows(
            monkeypatch,
            [(1, False, "Hidden", 10), (2, True, "", 10)],
            process_ids={10: _TERMINAL_ID},
            window_ids={},
        )
        assert input_proc._find_window_by_app_id(_TERMINAL_ID, MagicMock()) is None

    def test_an_enumeration_failure_is_swallowed(self, monkeypatch):
        def boom(callback, extra):
            raise OSError("window destroyed during enumeration")

        monkeypatch.setattr(input_proc.win32gui, "EnumWindows", boom)
        assert input_proc._find_window_by_app_id(_TERMINAL_ID, MagicMock()) is None

    def test_a_reader_that_raises_skips_only_that_window(self, monkeypatch):
        self._windows(
            monkeypatch,
            [(1, True, "Bad", 10), (2, True, "Good", 20)],
            process_ids={20: _TERMINAL_ID},
            window_ids={},
        )

        def process_id(pid):
            if pid == 10:
                raise OSError("access denied")
            return _TERMINAL_ID

        monkeypatch.setattr(input_proc, "_process_app_id", process_id)
        assert input_proc._find_window_by_app_id(_TERMINAL_ID, MagicMock()) == 2


class _FakeKernel32:
    """A stand-in for kernel32 that reports one process's app ID or an error."""

    def __init__(self, result, app_id="", opens=True):
        self.result = result
        self.app_id = app_id
        self.opens = opens
        self.closed = []
        self.opened_with = None

    def OpenProcess(self, access, inherit, pid):
        self.opened_with = (access, inherit, pid)
        return 4242 if self.opens else 0

    def GetApplicationUserModelId(self, handle, length, buffer):
        # The real call leaves the buffer alone on an error. This stand-in
        # fills it whatever it returns, so a caller that ignores the return
        # code reads an ID out of a failed call and a test can see it.
        buffer.value = self.app_id
        return self.result

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class TestProcessAppId:
    def test_a_packaged_process_reports_its_app_id(self, monkeypatch):
        fake = _FakeKernel32(0, _TERMINAL_ID)
        monkeypatch.setattr(input_proc, "_app_model_kernel32", lambda: fake)
        assert input_proc._process_app_id(1234) == _TERMINAL_ID
        assert fake.opened_with == (0x1000, False, 1234)
        assert fake.closed == [4242]

    def test_a_desktop_process_reports_none_and_is_closed(self, monkeypatch):
        # APPMODEL_ERROR_NO_APPLICATION, with text left in the buffer
        fake = _FakeKernel32(15703, _TERMINAL_ID)
        monkeypatch.setattr(input_proc, "_app_model_kernel32", lambda: fake)
        assert input_proc._process_app_id(1234) is None
        assert fake.closed == [4242]

    def test_a_packaged_process_with_an_empty_app_id_reports_none(self, monkeypatch):
        fake = _FakeKernel32(0, "")
        monkeypatch.setattr(input_proc, "_app_model_kernel32", lambda: fake)
        assert input_proc._process_app_id(1234) is None
        assert fake.closed == [4242]

    def test_a_process_that_cannot_be_opened_reports_none(self, monkeypatch):
        fake = _FakeKernel32(0, _TERMINAL_ID, opens=False)
        monkeypatch.setattr(input_proc, "_app_model_kernel32", lambda: fake)
        assert input_proc._process_app_id(1234) is None
        assert fake.closed == []


# ---------------------------------------------------------------------------
# wh-activate-windows-terminal.3: a bring-forward that Windows accepts
# ---------------------------------------------------------------------------
#
# David's attempts at 18:3x on 2026-09-29 logged "already running; brought
# its window forward" while Windows Terminal stayed behind. The cause was
# _activate_window_impl: it attached the calling thread to the TARGET
# window's thread, and the foreground lock refuses that (pywintypes error
# code 0). The fix brings the window forward through utils.foreground's
# steal_foreground, which attaches to the CURRENT FOREGROUND window's
# thread and detaches in a finally.

_CURRENT_THREAD = 11
_FOREGROUND_THREAD = 22
_TARGET_THREAD = 33
_FOREGROUND_HWND = 0x100
_TARGET_HWND = 0x200


class _ForegroundRefusal(Exception):
    """What pywin32 raises when Windows refuses SetForegroundWindow."""


def _fake_win32(monkeypatch, *, set_foreground_ok=True, raise_on_refusal=False):
    """Fake every Win32 call a bring-forward can make, and record them.

    Both the pywin32 calls (win32api / win32process / win32gui) and the
    ctypes namespace utils.foreground uses are faked with one recorder,
    so the test observes the same calls whichever route the code takes.
    The foreground window belongs to _FOREGROUND_THREAD and the target
    to _TARGET_THREAD. Returns the list of (name, args) calls.
    """
    import win32api

    import utils.foreground

    calls = []

    def thread_of(hwnd):
        return _FOREGROUND_THREAD if hwnd == _FOREGROUND_HWND else _TARGET_THREAD

    def attach(current, other, flag):
        calls.append(("AttachThreadInput", (current, other, flag)))
        return 1

    def set_foreground_ctypes(hwnd):
        calls.append(("SetForegroundWindow", (hwnd,)))
        if not set_foreground_ok and raise_on_refusal:
            raise _ForegroundRefusal("refused")
        return 1 if set_foreground_ok else 0

    def set_foreground_pywin32(hwnd):
        calls.append(("SetForegroundWindow", (hwnd,)))
        if not set_foreground_ok:
            # pywin32 raises on a refusal; it never returns 0.
            raise _ForegroundRefusal("refused")
        return None

    def bring_to_top(hwnd):
        calls.append(("BringWindowToTop", (hwnd,)))
        return 1

    ops = {
        "GetForegroundWindow": lambda: _FOREGROUND_HWND,
        "GetCurrentThreadId": lambda: _CURRENT_THREAD,
        "GetWindowThreadProcessId": thread_of,
        "AttachThreadInput": attach,
        "SetForegroundWindow": set_foreground_ctypes,
        "BringWindowToTop": bring_to_top,
    }
    monkeypatch.setattr(utils.foreground, "default_win32_ops", lambda: ops)

    monkeypatch.setattr(win32api, "GetCurrentThreadId", lambda: _CURRENT_THREAD)
    monkeypatch.setattr(
        input_proc.win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (thread_of(hwnd), 1234),
    )
    monkeypatch.setattr(input_proc.win32process, "AttachThreadInput", attach)
    monkeypatch.setattr(input_proc.win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(input_proc.win32gui, "ShowWindow", lambda hwnd, cmd: None)
    monkeypatch.setattr(
        input_proc.win32gui, "SetForegroundWindow", set_foreground_pywin32
    )
    monkeypatch.setattr(input_proc.win32gui, "BringWindowToTop", bring_to_top)
    monkeypatch.setattr(
        input_proc.win32gui, "GetWindowText", lambda hwnd: "PowerShell"
    )
    return calls


def _attach_calls(calls):
    return [args for name, args in calls if name == "AttachThreadInput"]


def _logged(mock_method):
    return [str(c.args[0]) for c in mock_method.call_args_list]


class TestActivateWindowForegroundLock:
    """T1, T2: _activate_window_impl attaches to the foreground thread."""

    def test_attaches_to_the_foreground_thread_never_the_target_thread(
        self, monkeypatch
    ):
        calls = _fake_win32(monkeypatch)
        logger = MagicMock()

        assert input_proc._activate_window_impl(_TARGET_HWND, logger) is True

        assert _attach_calls(calls) == [
            (_CURRENT_THREAD, _FOREGROUND_THREAD, True),
            (_CURRENT_THREAD, _FOREGROUND_THREAD, False),
        ]
        assert ("SetForegroundWindow", (_TARGET_HWND,)) in calls
        assert any("Activated window" in m for m in _logged(logger.info))

    @pytest.mark.parametrize("raise_on_refusal", [False, True])
    def test_a_refusal_returns_false_warns_and_still_detaches(
        self, monkeypatch, raise_on_refusal
    ):
        calls = _fake_win32(
            monkeypatch,
            set_foreground_ok=False,
            raise_on_refusal=raise_on_refusal,
        )
        logger = MagicMock()

        assert input_proc._activate_window_impl(_TARGET_HWND, logger) is False

        assert _attach_calls(calls) == [
            (_CURRENT_THREAD, _FOREGROUND_THREAD, True),
            (_CURRENT_THREAD, _FOREGROUND_THREAD, False),
        ]
        warnings = _logged(logger.warning)
        assert len(warnings) == 1
        assert str(_TARGET_HWND) in warnings[0]
        assert "refused" in warnings[0]
        assert not any("Activated window" in m for m in _logged(logger.info))


class TestStoreBranchReportsTheRealOutcome:
    """T3: the Store branch acts on the bring-forward result."""

    def _run(self, monkeypatch, activated_ok):
        activated = []
        monkeypatch.setattr(
            input_proc,
            "_activate_window_impl",
            lambda hwnd, logger: activated.append(hwnd) or activated_ok,
        )
        _running_windows(monkeypatch, {_TERMINAL_ID: 777})
        started = []
        monkeypatch.setattr(
            os, "startfile", lambda t: started.append(t), raising=False
        )
        logger = MagicMock()
        notices = []
        input_proc._start_named_program(
            "terminal",
            logger,
            lambda title, message: notices.append(message),
            _terminal_lookup,
            None,
        )
        return activated, started, logger, notices

    def test_a_refused_bring_forward_is_logged_as_a_refusal(self, monkeypatch):
        activated, started, logger, notices = self._run(monkeypatch, False)

        assert activated == [777]
        assert started == []
        assert notices == []
        assert not any(
            "brought its window forward" in m for m in _logged(logger.info)
        )
        assert any(
            "Terminal is already running" in m and "refused" in m
            for m in _logged(logger.warning)
        )

    def test_an_accepted_bring_forward_keeps_the_existing_line(self, monkeypatch):
        activated, started, logger, notices = self._run(monkeypatch, True)

        assert activated == [777]
        assert started == []
        assert notices == []
        assert any(
            "Terminal is already running; brought its window forward" in m
            for m in _logged(logger.info)
        )
        assert not any("refused" in m for m in _logged(logger.warning))


class TestNonStoreActivateUsesTheCorrectedMethod:
    """T4, boss ruling R1: the command-loop activate path for a window the
    title or process search found goes through steal_foreground too."""

    def test_a_found_window_is_brought_forward_by_steal_foreground(
        self, monkeypatch, startfile_calls
    ):
        import utils.foreground

        _fake_win32(monkeypatch)
        stolen = []
        monkeypatch.setattr(
            utils.foreground,
            "steal_foreground",
            lambda hwnd, win32_ops=None: stolen.append(hwnd) or True,
        )
        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: 4242
        )
        input_proc._handle_activate_window(
            {"target": "notepad.exe"},
            None,
            MagicMock(),
            "activate_window",
            threading.Event(),
            50,
            10,
            {},
            MagicMock(),
            find_programs=lambda name: [],
        )
        assert stolen == [4242]
        assert startfile_calls == []


class _FakeClock:
    """A clock that moves only when the poll sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _fake_poll_timing(monkeypatch):
    """Poll every 1 s for at most 5 s, on a clock that moves only on sleep."""
    clock = _FakeClock()
    monkeypatch.setattr(input_proc, "_STORE_WINDOW_POLL_S", 1.0, raising=False)
    monkeypatch.setattr(input_proc, "_STORE_WINDOW_WAIT_S", 5.0, raising=False)
    monkeypatch.setattr(input_proc, "_store_poll_clock", clock, raising=False)
    monkeypatch.setattr(input_proc, "_store_poll_sleep", clock.sleep, raising=False)
    return clock


def _app_id_answers(monkeypatch, answers):
    """_find_window_by_app_id answers from ``answers`` in order, then None."""
    asked = []
    pending = list(answers)

    def find(app_id, logger):
        asked.append(app_id)
        return pending.pop(0) if pending else None

    monkeypatch.setattr(input_proc, "_find_window_by_app_id", find)
    return asked


def _record_activations(monkeypatch):
    activated = []
    monkeypatch.setattr(
        input_proc,
        "_activate_window_impl",
        lambda hwnd, logger: activated.append(hwnd) or True,
    )
    return activated


class TestAStartedStoreProgramIsBroughtForward:
    """T5, acceptance item 4: the window of a Store program that was just
    started is brought to the front once it appears."""

    def _run(self, monkeypatch, answers, lookup=_terminal_lookup):
        clock = _fake_poll_timing(monkeypatch)
        asked = _app_id_answers(monkeypatch, answers)
        activated = _record_activations(monkeypatch)
        started = []
        monkeypatch.setattr(
            os, "startfile", lambda t: started.append(t), raising=False
        )
        logger = MagicMock()
        notices = []
        input_proc._start_named_program(
            "terminal",
            logger,
            lambda title, message: notices.append(message),
            lookup,
            None,
        )
        return clock, asked, activated, started, logger, notices

    def test_the_new_window_is_brought_forward_when_it_appears(self, monkeypatch):
        # The first answer is the check before the start (nothing is
        # running). Then two polls see nothing and the third sees the new
        # window.
        clock, asked, activated, started, logger, notices = self._run(
            monkeypatch, [None, None, None, 888]
        )
        assert started == [_TERMINAL_TARGET]
        assert notices == ["Starting Terminal"]
        assert activated == [888]
        assert asked == [_TERMINAL_ID] * 4
        assert clock.slept == [1.0, 1.0]

    def test_a_window_that_never_appears_stops_at_the_bound(self, monkeypatch):
        clock, asked, activated, started, logger, notices = self._run(
            monkeypatch, []
        )
        assert started == [_TERMINAL_TARGET]
        assert activated == []
        assert notices == ["Starting Terminal"]
        # Polled more than once, and never past the 5-second bound.
        assert len(asked) > 2
        assert sum(clock.slept) <= 5.0
        assert all(s == 1.0 for s in clock.slept)
        assert any(
            "Terminal" in m and "no window" in m for m in _logged(logger.info)
        )

    def test_a_fallback_that_started_is_the_one_polled(self, monkeypatch):
        """The winner failed to start and the Store fallback started, so
        the poll waits for the fallback's app ID."""
        program = _program(
            "Terminal",
            "C:\\Start\\Terminal.lnk",
            fallbacks=(_store_program("Terminal", _TERMINAL_ID),),
        )
        _fake_poll_timing(monkeypatch)
        asked = _app_id_answers(monkeypatch, [None, 999])
        activated = _record_activations(monkeypatch)
        calls = _startfile_that_fails(monkeypatch, {"C:\\Start\\Terminal.lnk"})
        input_proc._start_named_program(
            "terminal", MagicMock(), None, lambda spoken: [program], None
        )
        assert calls == ["C:\\Start\\Terminal.lnk", _TERMINAL_TARGET]
        assert activated == [999]
        assert asked == [_TERMINAL_ID, _TERMINAL_ID]

    def test_a_program_that_is_not_a_store_program_is_not_polled(
        self, monkeypatch
    ):
        clock, asked, activated, started, logger, notices = self._run(
            monkeypatch,
            [],
            lookup=lambda spoken: [_program("Outlook", "C:\\Start\\Outlook.lnk")],
        )
        assert started == ["C:\\Start\\Outlook.lnk"]
        assert asked == []
        assert activated == []
        assert clock.slept == []


_WINDOW_AT_START = 0x5150
_OTHER_WINDOW = 0x6160
_NEW_WINDOW = 888


class TestAStartedWindowRespectsTheUsersNextChoice:
    """Boss ruling R4 (18:59): the foreground window is read when the
    launch starts, and the new window is brought forward only if the
    foreground window is still that same window when the new window
    appears. A voice user can say another command, for example "activate
    brave", during the wait; Terminal must not then take the focus from
    the window the user chose. If Windows already gave the new window the
    focus, that counts as done."""

    def _run(self, monkeypatch, foreground_answers):
        events = []
        pending = list(foreground_answers)

        def foreground():
            answer = pending.pop(0)
            events.append(("foreground", answer))
            return answer

        monkeypatch.setattr(
            input_proc, "_foreground_window", foreground, raising=False
        )
        _fake_poll_timing(monkeypatch)
        # Nothing running before the start, then two polls: one miss and
        # the new window.
        _app_id_answers(monkeypatch, [None, None, _NEW_WINDOW])
        activated = []

        def activate(hwnd, logger):
            events.append(("activate", hwnd))
            activated.append(hwnd)
            return True

        monkeypatch.setattr(input_proc, "_activate_window_impl", activate)

        def startfile(target):
            events.append(("start", target))

        monkeypatch.setattr(os, "startfile", startfile, raising=False)
        logger = MagicMock()
        notices = []
        input_proc._start_named_program(
            "terminal",
            logger,
            lambda title, message: notices.append(message),
            _terminal_lookup,
            None,
        )
        return events, activated, logger, notices

    @staticmethod
    def _started_lines(logger):
        return [m for m in _logged(logger.info) if m.startswith("Started Terminal")]

    def test_the_user_moved_to_another_window_so_it_is_not_brought_forward(
        self, monkeypatch
    ):
        events, activated, logger, notices = self._run(
            monkeypatch, [_WINDOW_AT_START, _OTHER_WINDOW]
        )
        assert activated == []
        assert notices == ["Starting Terminal"]
        lines = self._started_lines(logger)
        assert len(lines) == 1, lines
        assert "another window" in lines[0]
        assert "by itself" not in lines[0]
        # Read once before the start and once when the window appeared.
        assert events == [
            ("foreground", _WINDOW_AT_START),
            ("start", _TERMINAL_TARGET),
            ("foreground", _OTHER_WINDOW),
        ]
        assert not _logged(logger.warning)

    def test_windows_already_brought_the_new_window_forward(self, monkeypatch):
        events, activated, logger, notices = self._run(
            monkeypatch, [_WINDOW_AT_START, _NEW_WINDOW]
        )
        assert activated == []
        assert notices == ["Starting Terminal"]
        lines = self._started_lines(logger)
        assert len(lines) == 1, lines
        assert "by itself" in lines[0]
        assert "another window" not in lines[0]
        assert events == [
            ("foreground", _WINDOW_AT_START),
            ("start", _TERMINAL_TARGET),
            ("foreground", _NEW_WINDOW),
        ]
        assert not _logged(logger.warning)

    def test_an_unchanged_foreground_brings_the_new_window_forward(
        self, monkeypatch
    ):
        events, activated, logger, notices = self._run(
            monkeypatch, [_WINDOW_AT_START, _WINDOW_AT_START]
        )
        assert activated == [_NEW_WINDOW]
        assert notices == ["Starting Terminal"]
        assert self._started_lines(logger) == []
        assert events == [
            ("foreground", _WINDOW_AT_START),
            ("start", _TERMINAL_TARGET),
            ("foreground", _WINDOW_AT_START),
            ("activate", _NEW_WINDOW),
        ]


class TestARefusedActivationNeverReportsSuccess:
    """wh-activate-windows-terminal.3.2.1, boss ruling 20:21 option 1: when
    Windows refuses to bring a found window forward, an awaited activate
    (close-app, minimize-app, maximize-app) gets an error response, so the
    rule stops before its Alt+F4, Win+Down or Win+Up reaches the window in
    front. A plain activate carries no request id and stays unchanged."""

    def _drive(self, monkeypatch, *, request_id, brought_forward):
        import queue

        monkeypatch.setattr(
            input_proc, "_find_window_by_target", lambda t, logger: 4242
        )
        monkeypatch.setattr(
            input_proc,
            "_activate_window_impl",
            lambda hwnd, logger: brought_forward,
        )
        verified = []
        monkeypatch.setattr(
            input_proc,
            "_verify_window_activation",
            lambda *args, **kwargs: verified.append(args),
        )
        notices = []
        responses = queue.Queue()
        input_proc._handle_activate_window(
            {"target": "notepad.exe"},
            request_id,
            responses,
            "activate_window",
            threading.Event(),
            50,
            10,
            {},
            MagicMock(),
            notify=lambda title, message: notices.append(message),
            find_programs=lambda name: [],
        )
        sent = []
        while not responses.empty():
            sent.append(responses.get_nowait())
        return sent, verified, notices

    def test_an_awaited_refusal_answers_with_an_error(self, monkeypatch):
        sent, verified, _ = self._drive(
            monkeypatch, request_id="r1", brought_forward=False
        )
        assert len(sent) == 1
        assert sent[0]["request_id"] == "r1"
        assert sent[0]["error"]
        # wh-safety-word-free-commands: a refusal is marked, so the Logic
        # process treats it as data and not as an application error.
        assert sent[0]["refusal"] is True
        assert sent[0]["action"] == "activate_window"
        assert "status" not in sent[0]
        assert verified == []

    def test_an_awaited_refusal_shows_no_notice(self, monkeypatch):
        _, _, notices = self._drive(
            monkeypatch, request_id="r1", brought_forward=False
        )
        assert notices == []

    def test_a_plain_refusal_sends_nothing(self, monkeypatch):
        sent, verified, notices = self._drive(
            monkeypatch, request_id=None, brought_forward=False
        )
        assert sent == []
        assert verified == []
        assert notices == []

    def test_an_awaited_success_is_still_verified(self, monkeypatch):
        sent, verified, _ = self._drive(
            monkeypatch, request_id="r1", brought_forward=True
        )
        assert sent == []
        assert len(verified) == 1
