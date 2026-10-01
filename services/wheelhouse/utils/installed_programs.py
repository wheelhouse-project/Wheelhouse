"""Look a spoken program name up among the programs installed on this PC.

wh-activate-launch-fallback. "activate outlook" arrives in the
Input process as window-title words rather than an executable, so when no
open window matches there is nothing to run and the command used to do
nothing at all. This module answers the missing question: which installed
programs does the user mean by those words?

Three kinds of source, all of them things Windows already keeps:

* Start Menu shortcuts, under the user's own Programs folder and the
  all-users one. The shortcut's file name is what the user would say, and
  the shortcut itself is what to start -- os.startfile opens a .lnk, so
  nothing here has to read the shortcut's contents.
* The App Paths registry keys, which are how Windows resolves a bare
  "outlook.exe" typed into the Run box. The key name is the executable,
  so the spoken name is matched against it with the .exe removed.
* The Windows apps folder (shell:AppsFolder), which is the only place a
  program installed from the Microsoft Store is listed: it has no Start
  Menu shortcut of the first kind and no App Paths key
  (wh-activate-windows-terminal). Only its Store entries are read, the
  ones whose app ID has the form <package family name>!<app id>; the
  desktop programs in that folder are already covered above. A Store
  entry is started with "shell:AppsFolder", a backslash, and the app ID,
  all built from the app ID the folder itself returned and from nothing
  the user said.

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

# The four sources, named in the order that decides a repeated name:
# the user's own Start Menu wins over the all-users one, a Start Menu
# shortcut wins over an App Paths key, and both win over a Microsoft Store
# entry. _without_duplicates explains why.
USER_START_MENU = "user Start Menu"
COMMON_START_MENU = "all-users Start Menu"
APP_PATHS = "App Paths"
STORE_APPS = "Microsoft Store"

# The shell folder that lists every program the Start menu can start, and
# the prefix that makes one of its entries something os.startfile accepts.
_APPS_FOLDER = "shell:AppsFolder"
_STORE_TARGET_PREFIX = _APPS_FOLDER + "\\"

# Where Windows keeps the display name of each installed package, per user.
_PACKAGE_REPOSITORY_KEY = (
    r"Software\Classes\Local Settings\Software\Microsoft\Windows"
    r"\CurrentVersion\AppModel\Repository\Packages"
)

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
    source: which of the four sources reported it, which is what
    decides a repeated name.
    fallbacks: the same program as reported by the later sources that
    yielded to this one, in source order. A name is claimed on the
    existence of its shortcut alone -- nothing here resolves what the
    shortcut points at -- so these are what a failed launch tries next
    (wh-activate-launch-fallback.1.6). Empty for everything except the
    winner of a repeated name.
    app_id: the Store app ID (<package family name>!<app id>) of an entry
    from the Microsoft Store source, which is how a window the program
    already has open is recognised. Empty for every other source.
    """

    name: str
    launch_target: str
    source: str
    fallbacks: tuple = ()
    app_id: str = ""


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


def _store_programs_from_records(records):
    """Turn apps-folder records into lookup entries. Pure: reads nothing.

    Each record is (app ID, tile name, package display name or None,
    number of apps the folder lists for that package). Rules:

    * Only an app ID of the form <package family name>!<app id> is a Store
      program. Everything else in the apps folder is a desktop program
      that the first two sources already report. A "package family name"
      that holds a path separator or a colon is a path, not a package.
    * The name is the tile name, which is what the Start menu shows. The
      launch target is built from the app ID alone.
    * A package that lists exactly one app is also known by the name the
      Store gives the package, when that differs from the tile name:
      Windows Terminal shows as "Terminal" but is called "Windows
      Terminal" everywhere else. Both entries share one launch target, so
      the lookup never reads them as two programs. A package with several
      apps gets no such entry, because one package name would then claim
      every one of them.
    """
    found = []
    for app_id, tile_name, package_name, package_app_count in records:
        family, bang, _app = app_id.partition("!")
        if not bang or not tile_name or set(family) & set("\\/:"):
            continue
        target = _STORE_TARGET_PREFIX + app_id
        found.append(
            InstalledProgram(
                name=tile_name,
                launch_target=target,
                source=STORE_APPS,
                app_id=app_id,
            )
        )
        if (
            package_app_count == 1
            and package_name
            and package_name.casefold() != tile_name.casefold()
        ):
            found.append(
                InstalledProgram(
                    name=package_name,
                    launch_target=target,
                    source=STORE_APPS,
                    app_id=app_id,
                )
            )
    return found


def _resolve_package_display_name(package_full_name, raw_name):
    """The readable name of a package, or None when it cannot be resolved.

    ``raw_name`` is the DisplayName value the package repository holds.
    Plain text is used as it is. An ``ms-resource:`` reference, which is
    what most Store packages hold (Windows Terminal holds
    ms-resource:AppStoreName), is resolved with SHLoadIndirectString
    against the package's own resources.
    """
    if not raw_name:
        return None
    if not raw_name.lower().startswith("ms-resource:"):
        return raw_name
    package_name = package_full_name.split("_", 1)[0]
    if raw_name.lower().startswith("ms-resource://"):
        resource = raw_name
    else:
        rest = raw_name[len("ms-resource:"):].lstrip("/")
        if "/" in rest:
            resource = f"ms-resource://{package_name}/{rest}"
        else:
            resource = f"ms-resource://{package_name}/Resources/{rest}"
    import ctypes
    from ctypes import wintypes

    shlwapi = ctypes.WinDLL("shlwapi")
    load = shlwapi.SHLoadIndirectString
    load.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.UINT,
        ctypes.c_void_p,
    ]
    load.restype = wintypes.LONG
    buffer = ctypes.create_unicode_buffer(512)
    source = f"@{{{package_full_name}?{resource}}}"
    if load(source, buffer, len(buffer), None) != 0:
        return None
    resolved = buffer.value
    if not resolved or resolved.lower().startswith("ms-resource"):
        return None
    return resolved


def _package_display_name(app_id, shell, propsys):
    """The Store's name for the package an app ID belongs to, or None."""
    import winreg

    item = shell.SHCreateItemFromParsingName(
        _STORE_TARGET_PREFIX + app_id, None, shell.IID_IShellItem2
    )
    store = item.GetPropertyStore(0, propsys.IID_IPropertyStore)
    key = propsys.PSGetPropertyKeyFromName("System.AppUserModel.PackageFullName")
    full_name = store.GetValue(key).GetValue()
    if not full_name:
        return None
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _PACKAGE_REPOSITORY_KEY + "\\" + full_name
    ) as package_key:
        raw_name, _kind = winreg.QueryValueEx(package_key, "DisplayName")
    return _resolve_package_display_name(full_name, raw_name)


def _enumerate_store_apps(shell, shellcon, propsys):
    """Do every shell call of the apps-folder read; return plain data.

    The result holds only str, int and None. No COM object may leave this
    function, so that every one of them is released when its frame ends.
    """
    desktop = shell.SHGetDesktopFolder()
    _, pidl, _ = desktop.ParseDisplayName(0, None, _APPS_FOLDER, 0)
    apps = desktop.BindToObject(pidl, None, shell.IID_IShellFolder)
    flags = shellcon.SHCONTF_NONFOLDERS | shellcon.SHCONTF_FOLDERS
    tiles = {}
    for item in apps.EnumObjects(0, flags):
        try:
            app_id = apps.GetDisplayNameOf(item, shellcon.SHGDN_FORPARSING)
            if "!" not in app_id:
                continue
            tile_name = apps.GetDisplayNameOf(item, shellcon.SHGDN_NORMAL)
        except Exception as exc:
            logger.debug("Apps-folder item unreadable, skipped: %s", str(exc))
            continue
        tiles.setdefault(app_id.casefold(), (app_id, tile_name))
    per_package = {}
    for app_id, _tile in tiles.values():
        family = app_id.partition("!")[0].casefold()
        per_package[family] = per_package.get(family, 0) + 1
    records = []
    for app_id, tile_name in tiles.values():
        count = per_package[app_id.partition("!")[0].casefold()]
        package_name = None
        if count == 1:
            try:
                package_name = _package_display_name(app_id, shell, propsys)
            except Exception as exc:
                logger.debug("Package name of %s unreadable: %s", app_id, str(exc))
        records.append((app_id, tile_name, package_name, count))
    return records


def _read_store_app_records():
    """Read the apps folder: (app ID, tile name, package name, package apps).

    Runs on the launch thread, which never initialised COM, so it does
    that itself and undoes it. The enumeration can return one item twice
    (Calculator did), so a repeated app ID is dropped. The package name is
    read only for a package that lists exactly one app, the only case its
    consumer uses it in, so every other app costs one folder read and
    nothing more.

    Every COM object must be released while COM is still initialised on
    the thread. So all shell calls run in _enumerate_store_apps, whose
    frame (and, after a failure, its traceback) is gone before
    CoUninitialize runs here.
    """
    import pythoncom
    from win32com.propsys import propsys
    from win32com.shell import shell, shellcon

    initialised = False
    try:
        pythoncom.CoInitialize()
        initialised = True
    except pythoncom.com_error:
        # COM is already initialised on this thread in another mode,
        # which is good enough for the shell calls below.
        pass
    try:
        records = _enumerate_store_apps(shell, shellcon, propsys)
    except Exception as exc:
        # The exception, and with it the traceback that references the
        # helper's frame, is deleted when this block ends. Log its text,
        # not the object: a log record (or a handler that keeps records)
        # would otherwise hold the traceback, and so the COM objects.
        logger.warning("Microsoft Store lookup unavailable: %s", str(exc))
        records = []
    if initialised:
        pythoncom.CoUninitialize()
    return records


def _store_programs():
    """Every Microsoft Store program the apps folder lists. Never raises."""
    try:
        return _store_programs_from_records(_read_store_app_records())
    except Exception as exc:
        logger.warning("Microsoft Store lookup unavailable: %s", str(exc))
        return []


def scan_installed_programs():
    """Every installed program this machine can report. Never raises.

    crewcut: the scan runs in full on every lookup, with no cache. It is
    reached only when a spoken name matched no open window, so the user
    is already waiting for a program to start, and a cache would have to
    notice newly installed programs to stay honest. The Store scan is the
    slowest part: it opens the apps folder and reads a package name for
    each single-app package. To remove the limit, cache the result and
    invalidate it on a directory-change notification for the two Start
    Menu folders (ReadDirectoryChangesW) and on a change to the package
    repository registry key (RegNotifyChangeKeyValue).
    """
    programs = []
    for source in (_start_menu_programs, _app_paths_programs, _store_programs):
        try:
            programs.extend(source())
        except Exception as exc:
            logger.warning("Installed-program source %s failed: %s", source, exc)
    return programs


def _without_duplicates(programs):
    """Drop the repeats of one program, keeping the earliest source.

    Programs arrive in source order: the user's Start Menu, then the
    all-users Start Menu, then App Paths, then the Microsoft Store. Three
    rules decide a repeat.

    * The same launch target twice is one program.
    * A name an earlier source already claimed yields. A Start Menu
      shortcut therefore beats an App Paths key of the same name (and
      either beats a Store entry of that name), and the user's own
      shortcut beats the all-users copy of it. The two
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
