"""Tests for the installed-program lookup (wh-activate-launch-fallback).

"x-ray activate outlook" reaches the Input process as a title pattern, not
an executable, so when no window matches there is nothing to run. This
module turns the spoken words into installed programs -- Start Menu
shortcuts and App Paths registry keys -- so the caller can start the one
that matches, or list the several that do.

The matching tests inject their own candidate list, through the ``scan=``
seam, precisely so the rules can be pinned on a machine whose installed
programs nobody controls. Two other groups do read real interfaces, and
each says so: TestStartMenuWalk redirects both Start Menu scopes to
temporary directories it builds, and TestRealScan runs the real scan
once to prove it never raises.
"""
import sys

import pytest

from utils.installed_programs import (
    APP_PATHS,
    COMMON_START_MENU,
    USER_START_MENU,
    InstalledProgram,
    find_installed_programs,
)


def _program(name, target=None, source=USER_START_MENU):
    return InstalledProgram(
        name=name,
        launch_target=target or f"C:\\{name}.lnk",
        source=source,
    )


def _find(spoken, candidates):
    return find_installed_programs(spoken, scan=lambda: list(candidates))


class TestMatching:
    def test_exact_name_matches(self):
        found = _find("Outlook", [_program("Outlook"), _program("Notepad")])
        assert [p.name for p in found] == ["Outlook"]

    def test_matching_ignores_case_in_both_directions(self):
        found = _find("OUTLOOK", [_program("outlook")])
        assert [p.name for p in found] == ["outlook"]

    def test_a_name_that_starts_with_the_spoken_words_matches(self):
        found = _find(
            "visual studio",
            [_program("Visual Studio Code"), _program("Notepad")],
        )
        assert [p.name for p in found] == ["Visual Studio Code"]

    def test_an_exact_match_wins_over_a_longer_name_that_also_starts_with_it(self):
        """Criterion 1: exact first, THEN prefix. Both exist here."""
        found = _find(
            "Code",
            [_program("Visual Studio Code"), _program("Code"), _program("Code Runner")],
        )
        assert [p.name for p in found] == ["Code"]

    def test_the_spoken_words_are_stripped_before_matching(self):
        found = _find("  outlook  ", [_program("Outlook")])
        assert [p.name for p in found] == ["Outlook"]

    def test_no_match_returns_nothing(self):
        found = _find("hyperion", [_program("Outlook"), _program("Notepad")])
        assert found == []

    def test_a_word_in_the_middle_of_a_name_does_not_match(self):
        """Prefix only. 'studio' must not pull in 'Visual Studio Code'."""
        found = _find("studio", [_program("Visual Studio Code")])
        assert found == []

    def test_empty_spoken_words_match_nothing(self):
        """Guard: every name starts with the empty string."""
        found = _find("   ", [_program("Outlook"), _program("Notepad")])
        assert found == []


class TestSeveralMatches:
    def test_every_program_that_starts_with_the_spoken_words_is_returned(self):
        found = _find(
            "note",
            [_program("Notepad"), _program("Notepad++"), _program("Outlook")],
        )
        assert [p.name for p in found] == ["Notepad", "Notepad++"]

    def test_the_same_program_found_twice_is_returned_once(self):
        """A program in both Start Menu scopes must not read as ambiguous."""
        found = _find(
            "outlook",
            [
                _program("Outlook", "C:\\Users\\me\\Outlook.lnk"),
                _program("Outlook", "C:\\Users\\me\\Outlook.lnk"),
            ],
        )
        assert [p.name for p in found] == ["Outlook"]

    def test_the_duplicate_check_ignores_the_case_of_the_path(self):
        found = _find(
            "outlook",
            [
                _program("Outlook", "C:\\Program Files\\Outlook.lnk"),
                _program("Outlook", "c:\\program files\\outlook.lnk"),
            ],
        )
        assert [p.name for p in found] == ["Outlook"]

    def test_one_name_in_two_folders_of_one_scope_stays_ambiguous(self):
        """Two different programs, one Start Menu scope, two subfolders.

        Windows keeps file names unique inside a folder, so the same
        shortcut name in one scope means two subfolders, and those can
        hold two different programs. The user picks by saying the full
        name, which is what the several-matches notice asks for.
        """
        found = _find(
            "outlook",
            [
                _program("Outlook", "C:\\Office\\Outlook.lnk"),
                _program("Outlook", "C:\\Other\\Outlook.lnk"),
            ],
        )
        assert len(found) == 2

    def test_an_app_paths_entry_yields_to_a_start_menu_shortcut(self):
        """Measured on the developer's machine for "excel" and "onenote".

        The shortcut and the registry key name one program, and their
        launch targets can never compare equal: one is a full .lnk path,
        the other a bare key name. Without this rule the notice lists the
        same name twice, and saying the full name returns the identical
        pair, so the user has no way out.
        """
        found = _find(
            "excel",
            [
                _program("Excel", "C:\\ProgramData\\Excel.lnk"),
                _program("excel", "excel.exe", source=APP_PATHS),
            ],
        )
        assert [p.launch_target for p in found] == ["C:\\ProgramData\\Excel.lnk"]

    def test_a_shortcut_in_both_scopes_yields_to_the_user_scope(self):
        """A program installed per-user and machine-wide, such as Chrome."""
        found = _find(
            "chrome",
            [
                _program("Chrome", "C:\\Users\\me\\Chrome.lnk"),
                _program(
                    "Chrome",
                    "C:\\ProgramData\\Chrome.lnk",
                    source=COMMON_START_MENU,
                ),
            ],
        )
        assert [p.launch_target for p in found] == ["C:\\Users\\me\\Chrome.lnk"]

    def test_the_all_users_scope_still_reaches_a_name_the_user_lacks(self):
        found = _find(
            "teams",
            [
                _program("Chrome", "C:\\Users\\me\\Chrome.lnk"),
                _program(
                    "Teams",
                    "C:\\ProgramData\\Teams.lnk",
                    source=COMMON_START_MENU,
                ),
            ],
        )
        assert [p.launch_target for p in found] == ["C:\\ProgramData\\Teams.lnk"]


class _FakeRegistryKey:
    """The smallest thing winreg.OpenKey can return that this module uses."""

    def __init__(self, entries, unreadable):
        self.entries = entries
        self.unreadable = unreadable

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeWinreg:
    """A stand-in for winreg holding one App Paths key with known entries.

    Only one root/view combination serves the key; the other three raise,
    which is what a real machine does when the per-user key was never
    written. That keeps the entry list in the result exactly once.
    """

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_WOW64_64KEY = 2
    KEY_WOW64_32KEY = 4

    def __init__(self, entries, unreadable=()):
        self.entries = list(entries)
        self.unreadable = set(unreadable)

    def OpenKey(self, root, sub_key, reserved, access):
        if root != self.HKEY_LOCAL_MACHINE or not access & self.KEY_WOW64_64KEY:
            raise OSError("key not found")
        return _FakeRegistryKey(self.entries, self.unreadable)

    def QueryInfoKey(self, key):
        return (len(key.entries), 0, 0)

    def EnumKey(self, key, index):
        name = key.entries[index]
        if name in key.unreadable:
            raise OSError("access denied")
        return name


class TestAppPathsEnumeration:
    """wh-activate-launch-fallback.1.3, the registry half.

    A key can be deleted between the count and the read, and an ACL can
    deny one key while allowing its parent. One unreadable entry must
    cost one entry, not every entry after it.
    """

    def test_one_unreadable_entry_does_not_drop_the_ones_after_it(
        self, monkeypatch
    ):
        from utils import installed_programs

        monkeypatch.setitem(
            sys.modules,
            "winreg",
            _FakeWinreg(["aaa.exe", "bad.exe", "zzz.exe"], unreadable={"bad.exe"}),
        )
        found = installed_programs._app_paths_programs()
        assert [p.name for p in found] == ["aaa", "zzz"]


class TestScanFailure:
    def test_a_failing_scan_matches_nothing_instead_of_raising(self):
        def boom():
            raise OSError("registry unavailable")

        # Written as a caught raise rather than a bare call so that a
        # regression fails this test on its own assertion. A test that
        # simply let the OSError escape would report an exception, and an
        # exception proves nothing about the behaviour being pinned.
        try:
            found = find_installed_programs("outlook", scan=boom)
        except OSError:
            pytest.fail("the lookup let the scan's failure escape")
        assert found == []


class TestRealScan:
    """The real scan, exercised once. It must never raise on this machine."""

    def test_the_real_scan_returns_programs_without_raising(self):
        from utils.installed_programs import scan_installed_programs

        programs = scan_installed_programs()
        assert isinstance(programs, list)
        assert all(isinstance(p, InstalledProgram) for p in programs)
        assert all(p.name and p.launch_target for p in programs)
        # The one environment-dependent assertion in this file, and it
        # earns its place: without it a failed win32com import or a
        # mistyped registry path returns [] and every other test here
        # still passes, because every other test injects its own list.
        # It claims nothing about other machines -- only that the machine
        # running the suite reports at least one installed program.
        assert programs, "the real scan found nothing: both sources are failing"


class TestStartMenuWalk:
    """The real walk, over a Start Menu tree the test builds itself.

    The two scope folders are redirected to temporary directories, so
    these pin the walk's own rules without reading the machine's real
    Start Menu.
    """

    def _redirect_scopes(self, monkeypatch, tmp_path):
        from win32com.shell import shell, shellcon

        user = tmp_path / "user-programs"
        common = tmp_path / "common-programs"
        (user / "Startup").mkdir(parents=True)
        (common / "StartUp").mkdir(parents=True)
        folders = {
            shellcon.CSIDL_PROGRAMS: user,
            shellcon.CSIDL_COMMON_PROGRAMS: common,
        }
        monkeypatch.setattr(
            shell,
            "SHGetFolderPath",
            lambda handle, csidl, token, flags: str(folders[csidl]),
        )
        return user, common

    def test_the_startup_folder_is_not_a_list_of_programs_to_start(
        self, monkeypatch, tmp_path
    ):
        """Startup holds copies of programs already in the tree.

        Measured on the developer's machine: "tailscale" returned both
        the Programs shortcut and the Startup copy, so the notice listed
        the same name twice and nothing could start.
        """
        from utils.installed_programs import _start_menu_programs

        user, _common = self._redirect_scopes(monkeypatch, tmp_path)
        (user / "Tailscale.lnk").write_text("")
        (user / "Startup" / "Tailscale.lnk").write_text("")

        found = _start_menu_programs()

        assert [p.launch_target for p in found] == [str(user / "Tailscale.lnk")]

    def test_the_all_users_startup_folder_is_skipped_too(
        self, monkeypatch, tmp_path
    ):
        from utils.installed_programs import _start_menu_programs

        _user, common = self._redirect_scopes(monkeypatch, tmp_path)
        (common / "StartUp" / "Updater.lnk").write_text("")

        assert _start_menu_programs() == []

    def test_each_shortcut_carries_the_scope_it_came_from(
        self, monkeypatch, tmp_path
    ):
        from utils.installed_programs import _start_menu_programs

        user, common = self._redirect_scopes(monkeypatch, tmp_path)
        (user / "Mine.lnk").write_text("")
        (common / "Ours.lnk").write_text("")

        found = _start_menu_programs()

        assert [(p.name, p.source) for p in found] == [
            ("Mine", USER_START_MENU),
            ("Ours", COMMON_START_MENU),
        ]

    def test_a_startup_folder_deeper_in_the_tree_is_an_ordinary_group(
        self, monkeypatch, tmp_path
    ):
        r"""Only Programs\Startup is the autostart folder.

        wh-activate-launch-fallback.1.4. Windows has exactly one autostart
        folder per scope, CSIDL_STARTUP, and it is the direct child
        "Startup" of the folder this walk begins at. A vendor group of the
        same name nested any deeper is a program group like any other, and
        dropping it hides every program the vendor put in it.
        """
        from utils.installed_programs import _start_menu_programs

        user, _common = self._redirect_scopes(monkeypatch, tmp_path)
        (user / "Acme" / "Startup").mkdir(parents=True)
        (user / "Acme" / "Startup" / "Acme Launcher.lnk").write_text("")

        found = _start_menu_programs()

        assert [p.name for p in found] == ["Acme Launcher"]


class _FakeWinregTwoViews:
    """A stand-in for winreg whose SECOND served key cannot be counted.

    wh-activate-launch-fallback.1.5. Two root/view pairs hold entries;
    QueryInfoKey raises for the second. The first pair's entries must
    survive that, because they were already collected.
    """

    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"
    KEY_READ = 1
    KEY_WOW64_64KEY = 2
    KEY_WOW64_32KEY = 4

    def __init__(self):
        self.served = {
            (self.HKEY_CURRENT_USER, self.KEY_WOW64_64KEY): ["first.exe"],
            (self.HKEY_LOCAL_MACHINE, self.KEY_WOW64_64KEY): ["second.exe"],
        }

    def OpenKey(self, root, sub_key, reserved, access):
        for view in (self.KEY_WOW64_64KEY, self.KEY_WOW64_32KEY):
            if access & view and (root, view) in self.served:
                return _FakeRegistryKey(self.served[(root, view)], set())
        raise OSError("key not found")

    def QueryInfoKey(self, key):
        if key.entries == ["second.exe"]:
            raise OSError("the key was deleted between the open and the count")
        return (len(key.entries), 0, 0)

    def EnumKey(self, key, index):
        return key.entries[index]


class TestAppPathsCountFailure:
    """One uncountable key must cost that key, not the whole scan."""

    def test_a_key_that_cannot_be_counted_keeps_the_earlier_entries(
        self, monkeypatch
    ):
        from utils import installed_programs

        monkeypatch.setitem(sys.modules, "winreg", _FakeWinregTwoViews())

        # Written as a caught raise, like TestScanFailure above and for the
        # same reason: a test that simply let the OSError escape would
        # report an exception, and an exception proves nothing about the
        # behaviour being pinned.
        try:
            found = installed_programs._app_paths_programs()
        except OSError:
            pytest.fail("an uncountable key let its failure escape the scan")

        assert [p.name for p in found] == ["first"]


class TestFallbackCandidates:
    """wh-activate-launch-fallback.1.6, ruled by the boss 2026-09-04.

    A name is claimed for the first source that reports it on the
    existence of its shortcut alone; nothing checks that the shortcut
    resolves. The entries that yield are kept on the winner so a failed
    launch can try them, in source order, before giving up.
    """

    def test_an_entry_that_yields_is_kept_as_a_fallback(self):
        found = _find(
            "excel",
            [
                _program("Excel", "C:\\Start\\Excel.lnk"),
                _program("excel", "excel.exe", source=APP_PATHS),
            ],
        )
        assert [p.launch_target for p in found] == ["C:\\Start\\Excel.lnk"]
        assert [f.launch_target for f in found[0].fallbacks] == ["excel.exe"]

    def test_the_fallbacks_keep_source_order(self):
        found = _find(
            "chrome",
            [
                _program("Chrome", "C:\\Users\\me\\Chrome.lnk"),
                _program(
                    "Chrome",
                    "C:\\ProgramData\\Chrome.lnk",
                    source=COMMON_START_MENU,
                ),
                _program("chrome", "chrome.exe", source=APP_PATHS),
            ],
        )
        assert [f.launch_target for f in found[0].fallbacks] == [
            "C:\\ProgramData\\Chrome.lnk",
            "chrome.exe",
        ]

    def test_an_ambiguous_same_scope_pair_carries_no_fallbacks(self):
        """The ambiguous pair never reaches a launch, so it needs none."""
        found = _find(
            "outlook",
            [
                _program("Outlook", "C:\\Office\\Outlook.lnk"),
                _program("Outlook", "C:\\Other\\Outlook.lnk"),
            ],
        )
        assert [p.fallbacks for p in found] == [(), ()]

    def test_only_the_winner_of_an_ambiguous_pair_carries_the_fallbacks(self):
        """Two same-scope shortcuts and a later source that yields to them.

        The pair stays ambiguous, so neither starts; the fallbacks belong
        to the first of them alone rather than being copied onto both.
        """
        found = _find(
            "outlook",
            [
                _program("Outlook", "C:\\Office\\Outlook.lnk"),
                _program("Outlook", "C:\\Other\\Outlook.lnk"),
                _program("outlook", "outlook.exe", source=APP_PATHS),
            ],
        )
        assert [len(p.fallbacks) for p in found] == [1, 0]

    def test_the_same_target_twice_is_not_a_fallback(self):
        """One program reported twice is one program, not two chances."""
        found = _find(
            "outlook",
            [
                _program("Outlook", "C:\\Start\\Outlook.lnk"),
                _program(
                    "Outlook",
                    "c:\\start\\outlook.lnk",
                    source=COMMON_START_MENU,
                ),
            ],
        )
        assert found[0].fallbacks == ()
