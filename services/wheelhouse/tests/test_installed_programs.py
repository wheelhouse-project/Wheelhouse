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
import gc
import logging
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


# ---------------------------------------------------------------------------
# wh-activate-windows-terminal: Microsoft Store programs
# ---------------------------------------------------------------------------

_TERMINAL_ID = "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App"
_STORE_PREFIX = "shell:AppsFolder\\"


def _store_records(records):
    from utils.installed_programs import _store_programs_from_records

    return _store_programs_from_records(records)


def _store_entries():
    """The entries the Store source reports for Windows Terminal."""
    return _store_records([(_TERMINAL_ID, "Terminal", "Windows Terminal", 1)])


class TestStoreRecords:
    """The rules that turn apps-folder records into lookup entries.

    "x-ray activate terminal" matched nothing: Windows Terminal is a Store
    program, so it has no Start Menu shortcut of the kind the first sources
    read and no App Paths key. The apps folder lists it. A record is
    (app ID, tile name, package display name or None, apps in the package).
    """

    def test_only_ids_with_an_exclamation_mark_are_store_apps(self):
        found = _store_records(
            [
                (_TERMINAL_ID, "Terminal", None, 1),
                ("C:\\Tools\\thing.exe", "Thing", None, 1),
                ("Microsoft.AutoGenerated.{ABC}", "Generated", None, 1),
            ]
        )
        assert [p.name for p in found] == ["Terminal"]

    def test_a_path_that_happens_to_hold_an_exclamation_mark_is_not_a_store_app(self):
        found = _store_records(
            [("C:\\Program Files\\Loud!\\loud.exe", "Loud", None, 1)]
        )
        assert found == []

    def test_the_launch_target_is_built_from_the_app_id_alone(self):
        found = _store_records([(_TERMINAL_ID, "Terminal", None, 1)])
        assert [p.launch_target for p in found] == [_STORE_PREFIX + _TERMINAL_ID]

    def test_an_entry_carries_its_source_and_app_id(self):
        from utils.installed_programs import STORE_APPS

        (program,) = _store_records([(_TERMINAL_ID, "Terminal", None, 1)])
        assert program.source == STORE_APPS
        assert program.app_id == _TERMINAL_ID

    def test_a_one_app_package_adds_an_entry_under_the_package_name(self):
        found = _store_records([(_TERMINAL_ID, "Terminal", "Windows Terminal", 1)])
        assert [p.name for p in found] == ["Terminal", "Windows Terminal"]
        assert {p.launch_target for p in found} == {_STORE_PREFIX + _TERMINAL_ID}
        assert {p.app_id for p in found} == {_TERMINAL_ID}

    def test_a_two_app_package_adds_no_package_name_entry(self):
        found = _store_records(
            [
                ("Vendor.Suite_abc!One", "One", "The Suite", 2),
                ("Vendor.Suite_abc!Two", "Two", "The Suite", 2),
            ]
        )
        assert [p.name for p in found] == ["One", "Two"]

    def test_a_package_name_equal_to_the_tile_name_adds_nothing(self):
        found = _store_records([(_TERMINAL_ID, "Terminal", "TERMINAL", 1)])
        assert [p.name for p in found] == ["Terminal"]

    def test_a_missing_package_name_adds_nothing(self):
        found = _store_records([(_TERMINAL_ID, "Terminal", None, 1)])
        assert [p.name for p in found] == ["Terminal"]

    def test_a_desktop_program_has_no_app_id(self):
        assert _program("Outlook").app_id == ""

    def test_a_record_with_no_tile_name_is_not_listed(self):
        assert _store_records([(_TERMINAL_ID, "", None, 1)]) == []

    @pytest.mark.parametrize(
        "app_id",
        [
            pytest.param("Vendor\\Pkg_1!App", id="backslash"),
            pytest.param("Vendor/Pkg_1!App", id="slash"),
            pytest.param("Vendor:Pkg_1!App", id="colon"),
        ],
    )
    def test_a_package_family_holding_a_path_character_is_not_a_store_app(
        self, app_id
    ):
        # One path character each, so removing any one of the three from
        # the guard is visible on its own.
        assert _store_records([(app_id, "Pkg", None, 1)]) == []


class TestStoreLookup:
    def test_terminal_finds_exactly_the_store_program(self):
        found = _find("terminal", _store_entries())
        assert [p.launch_target for p in found] == [_STORE_PREFIX + _TERMINAL_ID]

    def test_windows_terminal_finds_exactly_the_store_program(self):
        found = _find("windows terminal", _store_entries())
        assert [p.launch_target for p in found] == [_STORE_PREFIX + _TERMINAL_ID]

    def test_two_store_programs_sharing_a_name_stay_ambiguous(self):
        found = _find(
            "notes",
            _store_records(
                [
                    ("Vendor.A_1!App", "Notes", None, 1),
                    ("Vendor.B_2!App", "Notes", None, 1),
                ]
            ),
        )
        assert len(found) == 2

    def test_a_start_menu_shortcut_beats_the_store_entry_of_the_same_name(self):
        from utils.installed_programs import STORE_APPS

        found = _find(
            "terminal",
            [_program("Terminal", "C:\\Start\\Terminal.lnk")] + _store_entries(),
        )
        assert [p.launch_target for p in found] == ["C:\\Start\\Terminal.lnk"]
        assert [f.launch_target for f in found[0].fallbacks] == [
            _STORE_PREFIX + _TERMINAL_ID
        ]
        assert found[0].fallbacks[0].source == STORE_APPS
        assert found[0].fallbacks[0].app_id == _TERMINAL_ID


class TestScanSourceOrder:
    def test_the_store_source_runs_after_the_other_three(self, monkeypatch):
        from utils import installed_programs

        order = []

        def source(label):
            def read():
                order.append(label)
                return [_program(label)]

            return read

        monkeypatch.setattr(installed_programs, "_start_menu_programs", source("a"))
        monkeypatch.setattr(installed_programs, "_app_paths_programs", source("b"))
        monkeypatch.setattr(installed_programs, "_store_programs", source("c"))

        found = installed_programs.scan_installed_programs()

        assert order == ["a", "b", "c"]
        assert [p.name for p in found] == ["a", "b", "c"]

    def test_a_failing_store_source_keeps_the_others(self, monkeypatch):
        from utils import installed_programs

        def boom():
            raise OSError("apps folder unavailable")

        monkeypatch.setattr(
            installed_programs, "_start_menu_programs", lambda: [_program("a")]
        )
        monkeypatch.setattr(installed_programs, "_app_paths_programs", lambda: [])
        monkeypatch.setattr(installed_programs, "_store_programs", boom)

        found = installed_programs.scan_installed_programs()

        assert [p.name for p in found] == ["a"]

    def test_the_store_source_never_raises_when_the_reader_fails(self, monkeypatch):
        from utils import installed_programs

        def boom():
            raise OSError("apps folder unavailable")

        monkeypatch.setattr(installed_programs, "_read_store_app_records", boom)
        try:
            found = installed_programs._store_programs()
        except OSError:
            pytest.fail("the Store source let the reader's failure escape")
        assert found == []


_FULL_NAME = "Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe"


class _FakeShlwapi:
    """Stands in for the shlwapi DLL; records what it was asked to load."""

    def __init__(self, result=0, output="Windows Terminal"):
        self.sources = []
        self._result = result
        self._output = output

        def load(source, buffer, size, reserved):
            self.sources.append(source)
            buffer.value = self._output
            return self._result

        self.SHLoadIndirectString = load


class TestResolvePackageDisplayName:
    """_resolve_package_display_name, with the shlwapi DLL replaced.

    Most Store packages hold an ms-resource: reference instead of a name,
    and SHLoadIndirectString turns it into text. The stub records the
    string the function hands to it, which is the whole contract.
    """

    @staticmethod
    def _install(monkeypatch, **kwargs):
        import ctypes

        dll = _FakeShlwapi(**kwargs)
        loaded = []
        monkeypatch.setattr(
            ctypes,
            "WinDLL",
            lambda name, *a, **k: loaded.append(name) or dll,
            raising=False,
        )
        return dll, loaded

    @staticmethod
    def _resolve(raw_name, full_name=_FULL_NAME):
        from utils.installed_programs import _resolve_package_display_name

        return _resolve_package_display_name(full_name, raw_name)

    @pytest.mark.parametrize("raw", [None, ""])
    def test_an_empty_raw_name_resolves_to_nothing(self, monkeypatch, raw):
        dll, loaded = self._install(monkeypatch)
        assert self._resolve(raw) is None
        assert loaded == []

    def test_plain_text_is_returned_without_loading_the_dll(self, monkeypatch):
        dll, loaded = self._install(monkeypatch)
        assert self._resolve("Windows Terminal") == "Windows Terminal"
        assert loaded == []
        assert dll.sources == []

    def test_a_short_reference_names_the_resources_file_of_the_package(
        self, monkeypatch
    ):
        dll, _ = self._install(monkeypatch, output="Windows Terminal")
        assert self._resolve("ms-resource:AppStoreName") == "Windows Terminal"
        assert dll.sources == [
            "@{" + _FULL_NAME
            + "?ms-resource://Microsoft.WindowsTerminal/Resources/AppStoreName}"
        ]

    def test_a_full_reference_is_passed_through_unchanged(self, monkeypatch):
        dll, _ = self._install(monkeypatch)
        self._resolve("ms-resource://Other/Res/Name")
        assert dll.sources == ["@{" + _FULL_NAME + "?ms-resource://Other/Res/Name}"]

    def test_a_reference_with_a_path_uses_that_path_under_the_package(
        self, monkeypatch
    ):
        dll, _ = self._install(monkeypatch)
        self._resolve("ms-resource:Name/Sub")
        assert dll.sources == [
            "@{" + _FULL_NAME + "?ms-resource://Microsoft.WindowsTerminal/Name/Sub}"
        ]

    def test_a_failed_load_resolves_to_nothing(self, monkeypatch):
        self._install(monkeypatch, result=-2147024894)
        assert self._resolve("ms-resource:AppStoreName") is None

    def test_an_output_that_is_still_a_reference_resolves_to_nothing(
        self, monkeypatch
    ):
        self._install(monkeypatch, output="ms-resource:AppStoreName")
        assert self._resolve("ms-resource:AppStoreName") is None

    def test_an_empty_output_resolves_to_nothing(self, monkeypatch):
        self._install(monkeypatch, output="")
        assert self._resolve("ms-resource:AppStoreName") is None

    def test_a_leading_slash_is_dropped_from_a_reference_with_a_path(
        self, monkeypatch
    ):
        dll, _ = self._install(monkeypatch)
        self._resolve("ms-resource:/Name/Sub")
        assert dll.sources == [
            "@{" + _FULL_NAME + "?ms-resource://Microsoft.WindowsTerminal/Name/Sub}"
        ]


_SHGDN_NORMAL = 0
_SHGDN_FORPARSING = 4


class _FakeComObject:
    """A fake COM object that records the moment it is released."""

    def __init__(self, events, name):
        self._events = events
        self._name = name

    def __del__(self):
        self._events.append("released " + self._name)


class _FakeItem(_FakeComObject):
    def __init__(self, events, name, app_id, tile, fail_on=None):
        super().__init__(events, name)
        self.app_id = app_id
        self.tile = tile
        self.fail_on = fail_on


class _FakeApps(_FakeComObject):
    def __init__(self, events, make_items, raise_after=None):
        super().__init__(events, "apps")
        self._make_items = make_items
        self._raise_after = raise_after

    def EnumObjects(self, _hwnd, _flags):
        for item in self._make_items(self._events):
            yield item
        if self._raise_after is not None:
            # Built here, as the shell would: an exception kept by the test
            # would hold its own traceback and so the COM objects.
            raise self._raise_after()

    def GetDisplayNameOf(self, item, flags):
        if item.fail_on == flags:
            raise OSError("cannot read the name of " + item.app_id)
        return item.app_id if flags == _SHGDN_FORPARSING else item.tile


class _FakeDesktop(_FakeComObject):
    def __init__(self, events, apps):
        super().__init__(events, "desktop")
        self._apps = apps

    def ParseDisplayName(self, _hwnd, _bind, _name, _attrs):
        return 0, _FakeComObject(self._events, "pidl"), 0

    def BindToObject(self, _pidl, _bind, _iid):
        return self._apps


def _three_items(events, fail_on=None):
    return [
        _FakeItem(events, "item-a", "Vendor.A_1!App", "A"),
        _FakeItem(events, "item-b", "Vendor.B_2!App", "B", fail_on=fail_on),
        _FakeItem(events, "item-c", "Vendor.C_3!App", "C"),
    ]


class TestStoreReaderComLifetime:
    """_read_store_app_records releases every COM object before CoUninitialize.

    A COM object released after CoUninitialize can crash the process, so
    the function must not hold one at that point, on the normal path and
    when the enumeration fails part way. The stand-ins below replace
    pythoncom, win32com.shell and win32com.propsys through sys.modules.
    """

    @staticmethod
    def _install(monkeypatch, make_items, raise_after=None, init_error=False):
        import types

        events = []

        class _ComError(Exception):
            pass

        pythoncom = types.ModuleType("pythoncom")
        pythoncom.com_error = _ComError

        def co_initialize():
            if init_error:
                raise _ComError("COM is already initialised in another mode")
            events.append("CoInitialize")

        pythoncom.CoInitialize = co_initialize
        pythoncom.CoUninitialize = lambda: events.append("CoUninitialize")

        # The fakes are built inside the call, so the only references left
        # to them are the reader's own.
        shell = types.SimpleNamespace(
            SHGetDesktopFolder=lambda: _FakeDesktop(
                events, _FakeApps(events, make_items, raise_after)
            ),
            IID_IShellFolder=object(),
        )
        shellcon = types.SimpleNamespace(
            SHCONTF_NONFOLDERS=1,
            SHCONTF_FOLDERS=2,
            SHGDN_FORPARSING=_SHGDN_FORPARSING,
            SHGDN_NORMAL=_SHGDN_NORMAL,
        )
        win32com = types.ModuleType("win32com")
        win32com.__path__ = []
        shell_module = types.ModuleType("win32com.shell")
        shell_module.shell = shell
        shell_module.shellcon = shellcon
        propsys_module = types.ModuleType("win32com.propsys")
        propsys_module.propsys = types.SimpleNamespace()
        for name, module in (
            ("pythoncom", pythoncom),
            ("win32com", win32com),
            ("win32com.shell", shell_module),
            ("win32com.propsys", propsys_module),
        ):
            monkeypatch.setitem(sys.modules, name, module)

        from utils import installed_programs

        monkeypatch.setattr(
            installed_programs,
            "_package_display_name",
            lambda app_id, _shell, _propsys: "Package " + app_id,
        )
        return events

    @staticmethod
    def _assert_all_released_first(events, expected):
        assert "CoUninitialize" in events
        stop = events.index("CoUninitialize")
        released_before = sorted(e for e in events[:stop] if e.startswith("released"))
        released_all = sorted(e for e in events if e.startswith("released"))
        assert released_before == released_all, (
            "COM objects released after CoUninitialize: " + repr(events)
        )
        assert released_all == sorted("released " + n for n in expected)

    def test_every_object_is_released_before_couninitialize(self, monkeypatch):
        from utils.installed_programs import _read_store_app_records

        events = self._install(monkeypatch, _three_items)
        records = _read_store_app_records()
        assert len(records) == 3
        self._assert_all_released_first(
            events, ["desktop", "pidl", "apps", "item-a", "item-b", "item-c"]
        )

    def test_the_failure_path_releases_first_and_returns_an_empty_list(
        self, monkeypatch
    ):
        from utils.installed_programs import _read_store_app_records

        events = self._install(
            monkeypatch,
            lambda ev: [_FakeItem(ev, "item-a", "Vendor.A_1!App", "A")],
            raise_after=lambda: RuntimeError("enumeration broke"),
        )
        try:
            _read_store_app_records()
        except RuntimeError:
            pass
        gc.collect()  # a leftover traceback must not hide a late release
        self._assert_all_released_first(
            events, ["desktop", "pidl", "apps", "item-a"]
        )

    def test_a_failed_enumeration_yields_no_records_and_does_not_raise(
        self, monkeypatch
    ):
        from utils.installed_programs import _read_store_app_records

        self._install(
            monkeypatch,
            lambda ev: [_FakeItem(ev, "item-a", "Vendor.A_1!App", "A")],
            raise_after=lambda: RuntimeError("enumeration broke"),
        )
        assert _read_store_app_records() == []

    @pytest.mark.parametrize("failing_read", [_SHGDN_FORPARSING, _SHGDN_NORMAL])
    def test_one_item_whose_name_cannot_be_read_does_not_end_the_read(
        self, monkeypatch, failing_read
    ):
        from utils.installed_programs import _read_store_app_records

        self._install(
            monkeypatch, lambda ev: _three_items(ev, fail_on=failing_read)
        )
        records = _read_store_app_records()
        assert [r[0] for r in records] == ["Vendor.A_1!App", "Vendor.C_3!App"]

    def test_a_repeated_app_id_is_read_once(self, monkeypatch):
        # The enumeration returned Calculator twice. The second spelling
        # differs in case only, which the dedupe must also see.
        from utils.installed_programs import _read_store_app_records

        self._install(
            monkeypatch,
            lambda ev: [
                _FakeItem(ev, "item-a", "Vendor.A_1!App", "A"),
                _FakeItem(ev, "item-a-again", "VENDOR.A_1!APP", "A"),
            ],
        )
        assert _read_store_app_records() == [
            ("Vendor.A_1!App", "A", "Package Vendor.A_1!App", 1)
        ]

    def test_a_two_app_package_is_counted_and_its_name_is_not_read(
        self, monkeypatch
    ):
        from utils.installed_programs import _read_store_app_records

        self._install(
            monkeypatch,
            lambda ev: [
                _FakeItem(ev, "item-one", "Vendor.S_1!One", "One"),
                _FakeItem(ev, "item-two", "Vendor.S_1!Two", "Two"),
            ],
        )
        assert _read_store_app_records() == [
            ("Vendor.S_1!One", "One", None, 2),
            ("Vendor.S_1!Two", "Two", None, 2),
        ]

    def test_a_package_name_that_cannot_be_read_leaves_the_record_in(
        self, monkeypatch
    ):
        from utils import installed_programs

        self._install(
            monkeypatch,
            lambda ev: [_FakeItem(ev, "item-a", "Vendor.A_1!App", "A")],
        )

        def unreadable(_app_id, _shell, _propsys):
            raise OSError("the package has no repository entry")

        monkeypatch.setattr(installed_programs, "_package_display_name", unreadable)
        assert installed_programs._read_store_app_records() == [
            ("Vendor.A_1!App", "A", None, 1)
        ]

    # The WheelHouse queue handler keeps each log record unformatted until
    # its listener thread writes it, so a record whose arguments hold an
    # exception keeps that exception's traceback, and every apps-folder COM
    # object in its frames, alive past CoUninitialize. The record must carry
    # the text of the failure only.
    @staticmethod
    def _assert_no_record_holds_an_exception(caplog):
        assert caplog.records, "the failure was not logged at all"
        for record in caplog.records:
            held = [a for a in (record.args or ()) if isinstance(a, BaseException)]
            assert held == [], f"{record.getMessage()!r} holds {held!r}"

    @pytest.mark.parametrize("failing_read", [_SHGDN_FORPARSING, _SHGDN_NORMAL])
    def test_an_unreadable_item_is_logged_as_text_only(
        self, monkeypatch, caplog, failing_read
    ):
        from utils.installed_programs import _read_store_app_records

        self._install(
            monkeypatch, lambda ev: _three_items(ev, fail_on=failing_read)
        )
        with caplog.at_level(logging.DEBUG, logger="utils.installed_programs"):
            _read_store_app_records()
        self._assert_no_record_holds_an_exception(caplog)

    def test_an_unreadable_package_name_is_logged_as_text_only(
        self, monkeypatch, caplog
    ):
        from utils import installed_programs

        self._install(
            monkeypatch,
            lambda ev: [_FakeItem(ev, "item-a", "Vendor.A_1!App", "A")],
        )

        def unreadable(_app_id, _shell, _propsys):
            raise OSError("the package has no repository entry")

        monkeypatch.setattr(installed_programs, "_package_display_name", unreadable)
        with caplog.at_level(logging.DEBUG, logger="utils.installed_programs"):
            installed_programs._read_store_app_records()
        self._assert_no_record_holds_an_exception(caplog)

    def test_a_failed_store_lookup_is_logged_as_text_only(
        self, monkeypatch, caplog
    ):
        from utils import installed_programs

        def broken():
            raise RuntimeError("the apps folder could not be read")

        monkeypatch.setattr(installed_programs, "_read_store_app_records", broken)
        with caplog.at_level(logging.DEBUG, logger="utils.installed_programs"):
            assert installed_programs._store_programs() == []
        self._assert_no_record_holds_an_exception(caplog)

    def test_a_thread_whose_com_was_already_initialised_still_reads(
        self, monkeypatch
    ):
        from utils.installed_programs import _read_store_app_records

        events = self._install(monkeypatch, _three_items, init_error=True)
        try:
            records = _read_store_app_records()
        except Exception as exc:
            pytest.fail("the reader let the CoInitialize failure escape: " + str(exc))
        assert len(records) == 3
        # It never initialised COM, so it must not uninitialise it.
        assert "CoUninitialize" not in events
