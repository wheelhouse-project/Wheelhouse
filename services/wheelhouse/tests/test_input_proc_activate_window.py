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
