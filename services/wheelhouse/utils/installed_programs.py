"""Look a spoken program name up among the programs installed on this PC.

wh-activate-launch-fallback. "x-ray activate outlook" arrives in the
Input process as a title pattern rather than an executable, so when no
open window matches there is nothing to run and the command used to do
nothing at all. This module answers the missing question: which installed
programs does the user mean by those words?

Two sources, both of them things Windows already keeps:

* Start Menu shortcuts, under the user's own Programs folder and the
  all-users one. The shortcut's file name is what the user would say, and
  the shortcut itself is what to start -- os.startfile opens a .lnk, so
  nothing here has to read the shortcut's contents.
* The App Paths registry keys, which are how Windows resolves a bare
  "outlook.exe" typed into the Run box. The key name is the executable,
  so the spoken name is matched against it with the .exe removed.

The scan is a seam (the ``scan=`` argument) so the matching rules can be
tested against a fixed list. A machine's real Start Menu is nobody's to
control, and a test that depended on it would pass or fail by accident.
"""
import logging
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

# Where Windows resolves a bare executable name from, and the two views of
# it: a 32-bit program on a 64-bit Windows registers under WOW6432Node, and
# a reader that asks for only one view silently misses half the machine.
_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"

# The three sources, named in the order that decides a repeated name:
# the user's own Start Menu wins over the all-users one, and a Start Menu
# shortcut wins over an App Paths key. _without_duplicates explains why.
USER_START_MENU = "user Start Menu"
COMMON_START_MENU = "all-users Start Menu"
APP_PATHS = "App Paths"

# Programs\Startup is the autostart list, not a list of programs to start
# on request: every shortcut in it copies one from the tree above it.
# Measured on the developer's machine, "tailscale" returned the Programs
# shortcut and the Startup copy, so the notice listed one name twice and
# nothing started.
_AUTOSTART_FOLDER = "startup"


@dataclass(frozen=True)
class InstalledProgram:
    """One program the user could mean.

    name: what the user would say, and what a notice lists.
    launch_target: what to hand to os.startfile.
    source: which of the three sources reported it, which is what
    decides a repeated name.
    fallbacks: the same program as reported by the later sources that
    yielded to this one, in source order. A name is claimed on the
    existence of its shortcut alone -- nothing here resolves what the
    shortcut points at -- so these are what a failed launch tries next
    (wh-activate-launch-fallback.1.6). Empty for everything except the
    winner of a repeated name.
    """

    name: str
    launch_target: str
    source: str
    fallbacks: tuple = ()


def _start_menu_programs():
    """Every Start Menu shortcut, user folder first. Never raises."""
    found = []
    try:
        from win32com.shell import shell, shellcon
    except Exception as exc:
        logger.warning("Start Menu lookup unavailable: %s", exc)
        return found

    for csidl, source in (
        (shellcon.CSIDL_PROGRAMS, USER_START_MENU),
        (shellcon.CSIDL_COMMON_PROGRAMS, COMMON_START_MENU),
    ):
        try:
            folder = Path(shell.SHGetFolderPath(0, csidl, 0, 0))
        except Exception as exc:
            logger.warning("Start Menu folder %s unreadable: %s", csidl, exc)
            continue
        try:
            shortcuts = sorted(folder.rglob("*.lnk"))
        except OSError as exc:
            logger.warning("Start Menu folder %s could not be read: %s", folder, exc)
            continue
        for shortcut in shortcuts:
            folders_above = shortcut.relative_to(folder).parts[:-1]
            # The FIRST component only. There is exactly one autostart
            # folder per scope, CSIDL_STARTUP, and it is the direct child
            # "Startup" of the folder this walk begins at. A vendor group
            # of that name nested any deeper is an ordinary program group,
            # and skipping it hid every program in it
            # (wh-activate-launch-fallback.1.4).
            if folders_above and folders_above[0].casefold() == _AUTOSTART_FOLDER:
                continue
            found.append(
                InstalledProgram(
                    name=shortcut.stem,
                    launch_target=str(shortcut),
                    source=source,
                )
            )
    return found


def _app_paths_programs():
    """Every App Paths entry, both registry views. Never raises."""
    found = []
    try:
        import winreg
    except Exception as exc:  # pragma: no cover - winreg ships with Windows
        logger.warning("App Paths lookup unavailable: %s", exc)
        return found

    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in views:
            try:
                key = winreg.OpenKey(
                    root, _APP_PATHS_KEY, 0, winreg.KEY_READ | view
                )
            except OSError:
                # A missing App Paths key is normal, not a failure: the
                # per-user one does not exist until something writes it.
                continue
            with key:
                try:
                    entry_count = winreg.QueryInfoKey(key)[0]
                except OSError as exc:
                    # This view, not the whole scan. The count can fail
                    # for the same reasons the read below can -- the key
                    # deleted between the open and the count is the
                    # ordinary one -- and letting it escape here would
                    # discard every entry already collected from the
                    # earlier root/view pairs, because
                    # scan_installed_programs catches per SOURCE and this
                    # function's list dies with the call
                    # (wh-activate-launch-fallback.1.5).
                    logger.warning("App Paths key could not be counted: %s", exc)
                    continue
                for index in range(entry_count):
                    try:
                        entry = winreg.EnumKey(key, index)
                    except OSError as exc:
                        # One entry, not the rest: a key can be deleted
                        # between the count and the read, and an ACL can
                        # deny one key while allowing its parent. Stopping
                        # here would report every program after it as not
                        # installed (wh-activate-launch-fallback.1.3).
                        logger.warning("App Paths entry unreadable: %s", exc)
                        continue
                    name = entry[:-4] if entry.lower().endswith(".exe") else entry
                    found.append(
                        InstalledProgram(
                            name=name,
                            launch_target=entry,
                            source=APP_PATHS,
                        )
                    )
    return found


def scan_installed_programs():
    """Every installed program this machine can report. Never raises.

    crewcut: the scan runs in full on every lookup, with no cache. It is
    reached only when a spoken name matched no open window, so the user
    is already waiting for a program to start, and a cache would have to
    notice newly installed programs to stay honest. To remove the limit,
    cache the result and invalidate it on a directory-change notification
    for the two Start Menu folders (ReadDirectoryChangesW).
    """
    programs = []
    for source in (_start_menu_programs, _app_paths_programs):
        try:
            programs.extend(source())
        except Exception as exc:
            logger.warning("Installed-program source %s failed: %s", source, exc)
    return programs


def _without_duplicates(programs):
    """Drop the repeats of one program, keeping the earliest source.

    Programs arrive in source order: the user's Start Menu, then the
    all-users Start Menu, then App Paths. Three rules decide a repeat.

    * The same launch target twice is one program.
    * A name an earlier source already claimed yields. A Start Menu
      shortcut therefore beats an App Paths key of the same name, and
      the user's own shortcut beats the all-users copy of it. The two
      cases have the same cause: one program reported twice with launch
      targets that can never compare equal, because a shortcut carries a
      full .lnk path and a registry key carries a bare executable name.
      Measured on the developer's machine, "excel", "onenote" and
      "tailscale" each returned two entries, so the notice listed one
      name twice, saying the full name returned the identical pair, and
      the user had no way out.
    * The same name twice within ONE source stays. Windows keeps file
      names unique inside a folder, so this means two subfolders of one
      Start Menu scope, which can hold two different programs. Saying
      the full name is a real choice there.

    Ruled by David 2026-09-04 and overridable by him: these rules decide
    which program starts when the spoken name reaches more than one.
    """
    seen_targets = set()
    claimed_by = {}
    winner_by_name = {}
    displaced = {}
    unique = []
    for program in programs:
        target = program.launch_target.casefold()
        if target in seen_targets:
            continue
        name = program.name.casefold()
        seen_targets.add(target)
        if claimed_by.get(name, program.source) != program.source:
            displaced.setdefault(name, []).append(program)
            continue
        claimed_by.setdefault(name, program.source)
        winner_by_name.setdefault(name, program)
        unique.append(program)
    if not displaced:
        return unique
    kept = []
    for program in unique:
        name = program.name.casefold()
        yielded = displaced.get(name)
        # Only the winner carries them, so an ambiguous same-source pair
        # carries none: it never reaches a launch, it reaches the notice.
        if yielded and winner_by_name[name] is program:
            program = replace(program, fallbacks=tuple(yielded))
        kept.append(program)
    return kept


def find_installed_programs(spoken_name, *, scan=scan_installed_programs):
    """The installed programs the spoken words name.

    An exact name wins outright: when the user says "code" and something
    is called exactly that, the longer names that merely start with the
    word are not offered. Only when nothing matches exactly do the names
    that start with the spoken words count, so "visual studio" reaches
    "Visual Studio Code".

    Returns an empty list when the words name nothing, when they are
    blank, or when the machine cannot be scanned. The caller decides what
    to do with one match, several, or none.
    """
    wanted = (spoken_name or "").strip().casefold()
    if not wanted:
        return []

    try:
        candidates = scan()
    except Exception as exc:
        logger.warning("Installed-program lookup failed for %r: %s", spoken_name, exc)
        return []

    exact = []
    starts_with = []
    for program in candidates:
        name = program.name.casefold()
        if name == wanted:
            exact.append(program)
        elif name.startswith(wanted):
            starts_with.append(program)

    return _without_duplicates(exact or starts_with)
