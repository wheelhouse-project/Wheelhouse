"""Where a program or browser that Wheelhouse starts for the user begins.

Wheelhouse runs with services\\wheelhouse (inside the app folder) as its
current directory. A program that it starts inherits that directory, and the
program can outlive Wheelhouse. Windows then refuses to delete the app
folder, so an update fails (wh-installer-update-folder-locked). Every program
that Wheelhouse starts for the user is given a start folder outside the app
folder instead.

Not covered here: Wheelhouse's own child processes (the STT providers, the
AI server, the console probe). They end with Wheelhouse, and some of them
need the app folder as their current directory.
"""

import os
import sys
import tempfile
import webbrowser

_BROWSER_NAME = "wheelhouse-outside-app"


def launch_start_folder() -> str:
    """The folder to hand to a program that Wheelhouse starts for the user.

    The user profile folder; if that folder does not exist, the temporary
    folder. Never raises, and never returns a folder inside the app folder.
    """
    for candidate in (os.path.expanduser("~"), tempfile.gettempdir()):
        try:
            if candidate and os.path.isdir(candidate):
                return candidate
        except OSError:
            continue
    return os.environ.get("SystemRoot") or os.path.abspath(os.sep)


class OutsideAppBrowser(webbrowser.BaseBrowser):
    """The Windows default browser, started outside the app folder.

    webbrowser's own Windows default calls os.startfile(url), which gives the
    browser the current directory of this process. This one passes cwd. The
    return contract is the same: True when the shell accepted the address,
    False on OSError (the callers in main.py and speech/actions.py read it,
    wh-open-url-action.1.2).
    """

    def open(self, url, new=0, autoraise=True):
        sys.audit("webbrowser.open", url)
        try:
            os.startfile(url, cwd=launch_start_folder())
        except OSError:
            return False
        return True


def open_in_browser(url: str) -> bool:
    """Open an address in the default browser, outside the app folder.

    For the Qt windows, whose QDesktopServices.openUrl has no way to set a
    start folder. Returns what webbrowser.open returns.
    """
    ensure_browser_start_folder()
    return webbrowser.open(url)


def ensure_browser_start_folder() -> None:
    """Make webbrowser.open try OutsideAppBrowser first. Safe to repeat.

    Windows only. Call it in each process before it calls webbrowser.open.
    """
    if not hasattr(os, "startfile"):
        return
    try:
        if _BROWSER_NAME in webbrowser._browsers:
            return
    except AttributeError:
        pass
    webbrowser.register(_BROWSER_NAME, None, OutsideAppBrowser(), preferred=True)
