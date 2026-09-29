"""Parakeet model check and installer launch for the download offer.

When the user picks Parakeet and its speech model is not on the computer,
Wheelhouse offers to run the installer again with Parakeet chosen
(wh-parakeet-model-download-offer). This module holds the parts of that offer
that need no user interface:

- ``resolve_model_dir`` finds the model folder the Parakeet provider will
  load, in the provider's own order.
- ``model_state`` and ``is_model_complete`` apply the installer's
  completeness rule to that folder.
- ``locate_installer`` finds the stamped installer script the Setup program
  left on the computer, or returns the documented one-line command instead.
- ``build_launch_command`` and ``launch_installer`` start that script in a
  visible console once Wheelhouse has closed.

Read-only apart from ``launch_installer``. It imports nothing from Qt or from
the Logic process, and nothing from this package, because the release tests
(scripts/release/tests/test_installer.py) load this file by path in their
own environment to compare it with the installer's PowerShell.
"""
from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

logger = logging.getLogger(__name__)

PROVIDER_NAME = "parakeet_tdt"

# crewcut: DEFAULT_MODEL_DIRNAME, OVERRIDE_FILENAME and the order in
# resolve_model_dir are copies of the Parakeet provider's own
# (services/stt_providers/sherpa_offline_parakeet_stt_server/main.py,
# DEFAULT_MODEL_DIRNAME, OVERRIDE_FILENAME and _resolve_model_path). The
# provider runs in its own environment, so this process cannot import it.
# test_installer.py holds the two names to the provider's and the
# installer's values. Remove the copy by moving the resolver into a
# dependency-free module that both environments import.
DEFAULT_MODEL_DIRNAME = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3"
OVERRIDE_FILENAME = "stt_model_overrides.toml"

# The installer's names (scripts/release/public/install-wheelhouse.ps1
# $ShortcutName, and the Setup program's DefaultDirName
# {localappdata}\WheelhouseSetup in Wheelhouse-Setup.iss). Held to those
# files by test_installer.py.
SHORTCUT_NAME = "Wheelhouse.lnk"
SETUP_DIRNAME = "WheelhouseSetup"
INSTALLER_FILENAME = "install-wheelhouse.ps1"
UNSTAMPED_MARKER = "<ARCHIVE-SHA256>"

# The one-line install command as INSTALL.md documents it. It names the
# latest release, not a version: INSTALL.md documents no versioned form, and
# the command cannot carry -SttProvider, so the installer asks for the speech
# engine. Held to INSTALL.md by test_installer.py.
ONE_LINE_INSTALL_COMMAND = (
    "irm https://github.com/wheelhouse-project/Wheelhouse/releases/latest/"
    "download/install-wheelhouse.ps1 | iex"
)

# How long the launch console waits for Wheelhouse to close before it gives
# up without starting the installer. Wheelhouse's own launcher allows each of
# its three processes 5 seconds to exit and 5 more after terminate
# (launcher.py SHUTDOWN_GRACE_PERIOD_S), about 30 seconds at worst, and the
# speech provider stops before that. 120 seconds covers that four times over
# on a slow computer, and is still short enough that a user watching the
# console gets an answer.
WAIT_FOR_EXIT_SECONDS = 120

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

# crewcut: a copy of Test-RunningWheelHouse in install-wheelhouse.ps1 (the
# process names it asks WMI for, and the app folder it matches on the
# command line, $LocalRoot\app). The launch console waits until this test
# passes, because it is the test the installer refuses on. Remove the copy
# by having the installer accept a wait-for-exit parameter itself.
RUNNING_PROCESS_NAMES = ("python.exe", "pythonw.exe", "uv.exe")
APP_DIR_UNDER_LOCAL_APP_DATA = "Wheelhouse\\app"

# The one indirection tests replace, so no test starts a real process
# (pattern: stt/provider_env_check.py _run_uv).
_popen = subprocess.Popen


# --- Model folder -------------------------------------------------------------


def default_provider_dir() -> Path:
    """The Parakeet provider folder beside this app's own services folder."""
    # stt -> wheelhouse -> services -> repository or app root.
    root = Path(__file__).resolve().parents[3]
    return root / "services" / "stt_providers" / "sherpa_offline_parakeet_stt_server"


def _read_provider_config(provider_dir: Path) -> dict:
    """The provider's config.toml, or {} when it is absent or unreadable.

    The provider itself fails to start on a malformed config.toml. Here that
    only means no configured model path, so the coded default decides.
    """
    path = provider_dir / "config.toml"
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 -- the check must not raise
        logger.warning("Could not read the Parakeet provider config %s: %s", path, exc)
        return {}


def resolve_model_dir(provider_dir: Optional[Path] = None) -> Path:
    """The model folder the Parakeet provider would load right now.

    The order is the provider's (main.py _resolve_model_path): the
    [<provider name>] model_path in %LOCALAPPDATA%\\WheelHouse\\
    stt_model_overrides.toml, then [model].model_path in the provider's
    config.toml, then %LOCALAPPDATA%\\WheelHouse\\models\\
    <DEFAULT_MODEL_DIRNAME>. An override file that cannot be read or parsed,
    and a value that is not a string or is blank, are passed over, as the
    provider passes them over. The override file is read at every call.

    A relative path is joined to the provider folder, because the provider
    runs with that folder as its working directory (remote_stt_launcher.py
    starts it with cwd=service_dir). Never raises.
    """
    if provider_dir is None:
        provider_dir = default_provider_dir()
    provider_dir = Path(provider_dir)
    config = _read_provider_config(provider_dir)

    provider_section = config.get("provider", {})
    provider_name = (
        provider_section.get("name", PROVIDER_NAME)
        if isinstance(provider_section, dict)
        else PROVIDER_NAME
    )
    model_section = config.get("model", {})
    raw_configured = (
        model_section.get("model_path") if isinstance(model_section, dict) else None
    )
    if raw_configured is not None and not isinstance(raw_configured, str):
        raw_configured = None
    configured = (raw_configured or "").strip()

    local_app_data = os.environ.get("LOCALAPPDATA", "")

    override_value = ""
    if local_app_data:
        override_path = Path(local_app_data) / "WheelHouse" / OVERRIDE_FILENAME
        if override_path.exists():
            overrides = {}
            try:
                # utf-8-sig: PowerShell 5.1 writes a BOM by default.
                overrides = tomllib.loads(override_path.read_bytes().decode("utf-8-sig"))
            except Exception as exc:  # noqa: BLE001 -- as the provider does
                logger.warning(
                    "Ignoring unreadable model-path override file %s: %s",
                    override_path, exc,
                )
            section = overrides.get(provider_name)
            if isinstance(section, dict):
                raw_override = section.get("model_path")
                if raw_override is not None and not isinstance(raw_override, str):
                    raw_override = None
                override_value = (raw_override or "").strip()

    if override_value:
        chosen = override_value
    elif configured:
        chosen = configured
    elif local_app_data:
        return Path(local_app_data) / "WheelHouse" / "models" / DEFAULT_MODEL_DIRNAME
    else:
        # The provider leaves "" here, which names its working directory.
        chosen = ""

    path = Path(chosen)
    if not path.is_absolute():
        path = provider_dir / path
    return path


# --- Completeness rule ------------------------------------------------------

# crewcut: the rule below is a copy of Get-ModelState and Get-ModelFileState
# in scripts/release/public/install-wheelhouse.ps1. No data file holds it,
# so the parity tests in scripts/release/tests/test_installer.py run the
# PowerShell and this code on the same trees. Remove the copy by moving the
# required-file lists into a data file that both read.
_TOKENS = "tokens.txt"
_INT8_SET = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx")
_FULL_PRECISION_SET = ("encoder.onnx", "decoder.onnx", "joiner.onnx", "encoder.weights")

_FILE_ATTRIBUTE_HIDDEN = 0x2


def model_file_state(path: Union[str, Path]) -> str:
    """'ok', 'bad' or 'unreadable' for one required model entry.

    The installer's Get-ModelFileState: 'bad' is absent, empty, or not a
    regular file; 'unreadable' is an entry that could not be read. Measured
    on PowerShell 5.1.26100 and 7.6.6, the installer's Get-Item without
    -Force raises an IOException on a hidden entry (so 'unreadable'), and
    reports a symbolic link as a file of length 0 or a directory (so 'bad').
    lstat gives the same answers without following the link.
    """
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "bad"
    except OSError:
        return "unreadable"
    if getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_HIDDEN:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        return "bad"
    return "ok"


def model_state(model_dir: Union[str, Path]) -> str:
    """'complete', 'incomplete' or 'unreadable', as the installer decides.

    tokens.txt plus either the int8 trio, or the full-precision trio with
    encoder.weights. bpe.vocab is not required.
    """
    model_dir = Path(model_dir)
    saw_unreadable = False
    tokens = model_file_state(model_dir / _TOKENS)
    if tokens == "unreadable":
        saw_unreadable = True
    if tokens == "ok":
        for required in (_INT8_SET, _FULL_PRECISION_SET):
            all_present = True
            for name in required:
                state = model_file_state(model_dir / name)
                if state == "unreadable":
                    saw_unreadable = True
                if state != "ok":
                    all_present = False
                    break
            if all_present:
                return "complete"
    if saw_unreadable:
        return "unreadable"
    return "incomplete"


def is_model_complete(model_dir: Union[str, Path]) -> bool:
    """True only for a proven complete model; an unreadable entry is False."""
    return model_state(model_dir) == "complete"


def parakeet_model_complete(provider_dir: Optional[Path] = None) -> bool:
    """Resolve the model folder now and check it. Never raises."""
    try:
        return is_model_complete(resolve_model_dir(provider_dir))
    except Exception as exc:  # noqa: BLE001 -- an offer check must not raise
        logger.warning("Could not check the Parakeet model: %s", exc)
        return False


# --- Installer locator ------------------------------------------------------


@dataclass(frozen=True)
class InstallerLaunchPlan:
    """The stamped installer script to run."""

    script_path: Path


@dataclass(frozen=True)
class InstallerFallback:
    """No script to run: show the user this command instead.

    reason is one of no_setup_copy, unreadable, unstamped,
    unknown_app_version, version_mismatch (from locate_installer), or
    launch_failed (from main.py _handle_parakeet_download_now, when the
    launch console did not start).
    """

    command: str
    reason: str


_APP_VERSION_LINE = re.compile(r'^\$AppVersion\s*=\s*"([^"]*)"', re.M)


def setup_script_path() -> Optional[Path]:
    """%LOCALAPPDATA%\\WheelhouseSetup\\install-wheelhouse.ps1, or None."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None
    return Path(local_app_data) / SETUP_DIRNAME / INSTALLER_FILENAME


def locate_installer(
    app_version: Optional[str] = None,
) -> Union[InstallerLaunchPlan, InstallerFallback]:
    """The Setup program's stamped script when it installs this version.

    Only the copy Wheelhouse-Setup.exe leaves in %LOCALAPPDATA%\\
    WheelhouseSetup is used. The build workflow refuses to bundle a script
    that still holds the <ARCHIVE-SHA256> placeholder or names another
    version (build-installer.yml), and this checks both again, because an
    update through the one-line command leaves an older Setup copy behind,
    and running it would install the older release. Anything else returns
    the one-line command. Never raises.
    """
    if app_version is None:
        try:
            from utils.app_version import get_app_version

            app_version = get_app_version()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the Wheelhouse version: %s", exc)
            app_version = ""
    app_version = (app_version or "").strip()

    def fallback(reason: str) -> InstallerFallback:
        return InstallerFallback(command=ONE_LINE_INSTALL_COMMAND, reason=reason)

    script = setup_script_path()
    if script is None or not script.is_file():
        return fallback("no_setup_copy")
    try:
        text = script.read_bytes().decode("utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the Setup copy of the installer %s: %s", script, exc)
        return fallback("unreadable")
    if UNSTAMPED_MARKER in text:
        return fallback("unstamped")
    if not app_version or app_version == "unknown":
        return fallback("unknown_app_version")
    match = _APP_VERSION_LINE.search(text)
    if match is None or match.group(1).strip() != app_version:
        return fallback("version_mismatch")
    return InstallerLaunchPlan(script_path=script)


# --- Startup shortcut -------------------------------------------------------


def _startup_folder() -> Optional[Path]:
    """The folder [Environment]::GetFolderPath("Startup") names, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.WinDLL("shell32")
        get_folder = shell32.SHGetFolderPathW
        get_folder.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ]
        get_folder.restype = ctypes.HRESULT  # raises OSError on a failure code
        buffer = ctypes.create_unicode_buffer(260)
        csidl_startup = 0x0007
        get_folder(None, csidl_startup, None, 0, buffer)
        return Path(buffer.value) if buffer.value else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not find the Startup folder: %s", exc)
        return None


def startup_shortcut_path() -> Optional[Path]:
    """Where the installer puts the start-at-login shortcut.

    crewcut: a copy of install-wheelhouse.ps1's
    Join-Path ([Environment]::GetFolderPath("Startup")) $ShortcutName.
    Remove it by having the installer keep the current answer when
    -AutoStart is left out.
    """
    folder = _startup_folder()
    if folder is None:
        return None
    return folder / SHORTCUT_NAME


def startup_shortcut_exists() -> bool:
    path = startup_shortcut_path()
    try:
        return path is not None and path.exists()
    except OSError:
        return False


# --- Launch -----------------------------------------------------------------

# PowerShell ends a single-quoted string on any of these, not only on '.
_POWERSHELL_SINGLE_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")


def _ps_quote(text: str) -> str:
    for quote in _POWERSHELL_SINGLE_QUOTES:
        text = text.replace(quote, quote + quote)
    return "'" + text + "'"


def _powershell_exe() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )


def build_launch_command(
    script_path: Union[str, Path],
    pids: Iterable[int],
    *,
    auto_start: bool,
    timeout_seconds: int = WAIT_FOR_EXIT_SECONDS,
) -> list[str]:
    """The powershell.exe command that waits for Wheelhouse, then installs.

    The console first waits for the given process ids, then until no
    python.exe, pythonw.exe or uv.exe has the app folder on its command line
    (the installer's own Test-RunningWheelHouse), for at most
    timeout_seconds in all. If any given id or any such process is still
    running then, it says so,
    waits for a key, and exits without starting the installer. Otherwise it
    runs the script with -SttProvider parakeet_tdt -StartNow yes and the
    given -AutoStart, then waits for a key so the installer's last messages
    stay readable. When WMI cannot be asked, the given ids alone decide.

    The script is a single argument without double quotes, so the C runtime
    and powershell.exe read it the same way.
    """
    ids = []
    for pid in pids:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"process id must be a positive integer: {pid!r}")
        ids.append(str(pid))
    limit = int(timeout_seconds)
    if limit <= 0:
        raise ValueError("timeout_seconds must be positive")

    names_filter = " OR ".join(f"Name = ''{name}''" for name in RUNNING_PROCESS_NAMES)
    installer_call = (
        f"& {_ps_quote(str(script_path))} -SttProvider {PROVIDER_NAME} "
        f"-StartNow yes -AutoStart {'yes' if auto_start else 'no'}"
    )
    statements = [
        "$ids = @(" + ",".join(ids) + ")",
        f"$limit = {limit}",
        "$deadline = (Get-Date).AddSeconds($limit)",
        f"$appDir = Join-Path $env:LOCALAPPDATA {_ps_quote(APP_DIR_UNDER_LOCAL_APP_DATA)}",
        # The installer's Test-CommandLineInAppDir boundary: the app folder
        # followed by a separator, a quote, whitespace or the end.
        "$pattern = [regex]::Escape($appDir.ToLower()) + '($|[\\\\/' + [char]34 + '''\\s])'",
        "function Get-WheelhouseCount { "
        "$alive = @(); "
        "if ($ids.Count -gt 0) { $alive = @(Get-Process -Id $ids -ErrorAction SilentlyContinue) }; "
        f"try {{ $procs = @(Get-CimInstance Win32_Process -Filter '{names_filter}' -ErrorAction Stop) }} "
        "catch { return $alive.Count }; "
        "return $alive.Count + @($procs | Where-Object { $_.CommandLine -and ($_.CommandLine.ToLower() -match $pattern) }).Count "
        "}",
        "Write-Host 'Waiting for Wheelhouse to close.'",
        "if ($ids.Count -gt 0) { Wait-Process -Id $ids -Timeout $limit -ErrorAction SilentlyContinue }",
        "while (((Get-WheelhouseCount) -gt 0) -and ((Get-Date) -lt $deadline)) { Start-Sleep -Seconds 1 }",
        "if ((Get-WheelhouseCount) -gt 0) { "
        f"Write-Host 'Wheelhouse did not close within {limit} seconds, so the Wheelhouse installer was not started.'; "
        "Write-Host 'Exit Wheelhouse (right-click the Wheelhouse tray icon and choose Exit), then choose Parakeet again.'; "
        "Write-Host 'Press any key to close this window.'; "
        "try { $null = [Console]::ReadKey($true) } catch { }; "
        "exit 1 "
        "}",
        "Write-Host 'Starting the Wheelhouse installer.'",
        installer_call,
        "Write-Host ''",
        "Write-Host 'The Wheelhouse installer has stopped. Press any key to close this window.'",
        "try { $null = [Console]::ReadKey($true) } catch { }",
        # A caught error leaves $? false, and powershell.exe -Command would
        # then exit 1 after a finished run.
        "exit 0",
    ]
    wrapper = "; ".join(statements)
    return [
        _powershell_exe(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        wrapper,
    ]


def launch_installer(
    script_path: Union[str, Path],
    pids: Iterable[int],
    *,
    auto_start: Optional[bool] = None,
) -> bool:
    """Start the launch console and return at once. False if it did not start.

    auto_start None means: keep the current answer, read from whether the
    Startup shortcut exists now. The console gets its own window
    (CREATE_NEW_CONSOLE). Its working directory is the script's folder,
    because the installer deletes and rebuilds the app folder. No
    CREATE_BREAKAWAY_FROM_JOB: Wheelhouse puts only the local AI server in a
    job object (ai/server_launcher.py), not itself, so nothing ends this
    console when Wheelhouse exits.
    """
    if auto_start is None:
        auto_start = startup_shortcut_exists()
    script_path = Path(script_path)
    cmd = build_launch_command(script_path, pids, auto_start=auto_start)
    try:
        _popen(cmd, cwd=str(script_path.parent), creationflags=CREATE_NEW_CONSOLE)
    except Exception as exc:  # noqa: BLE001 -- the caller reports the failure
        logger.error("Could not start the Wheelhouse installer: %s", exc)
        return False
    logger.info("Started the Wheelhouse installer console for %s", script_path)
    return True
