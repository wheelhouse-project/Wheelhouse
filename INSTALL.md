# Installing WheelHouse

This guide covers installation in detail: what you need, what the
installer does, the optional speech engines, updating, troubleshooting,
and uninstalling. If you just want the short version, it is the one
command in the [README](./README.md).

## What you need

| Requirement | Details |
|-------------|---------|
| Windows 10 or 11, 64-bit | See the editions table below. |
| 10 GB of free disk space | The app, its Python environments, and the speech model. |
| 8 GB of memory (RAM) | Hard minimum for the built-in offline speech engine. 16 GB is recommended. Below 8 GB, the installer stops and suggests the Google Cloud engine, which runs in the cloud and needs far less memory. |
| 4 or more CPU cores | Recommended. With fewer, WheelHouse still installs, but speech recognition may respond slowly. |
| A microphone | You can plug one in after installing, but WheelHouse needs one to hear you. |
| An internet connection | For the install itself (roughly 1 GB of downloads). The default speech engine works offline after that. |

### Windows editions

| Windows version | Works? | Notes |
|-----------------|--------|-------|
| Windows 11 (any edition) | Yes | |
| Windows 10 with winget (most editions, 21H2 and later) | Yes | |
| Windows 10 LTSC or without winget | Yes | The installer detects that winget is missing and uses the official uv install script instead. |
| Windows 10 before version 1803 | Partly | `tar.exe` is missing on these editions. Install it yourself, or choose the Google Cloud engine (which needs no model archive). |
| 32-bit Windows, Windows 8.1 and older | No | The installer stops with an explanation. |

## Install

Open any PowerShell window (press the Windows key, type `powershell`,
press Enter) and run:

```powershell
irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

The whole process takes about 10 to 20 minutes, most of it downloading.
The installer:

1. Checks your computer meets the requirements above.
2. Installs uv, the Python environment manager WheelHouse uses. Nothing
   is installed into a system-wide Python; every environment lives inside
   WheelHouse's own folder.
3. Downloads the WheelHouse application archive and verifies its
   checksum.
4. Sets up the Python environments for the app and your speech engines.
5. Asks which speech engine you want (see below).
6. Downloads the offline speech model if you chose the default engine
   (about 650 MB — this is the longest step).
7. Writes your configuration and creates Start-menu and desktop
   shortcuts. It then asks two final questions: whether WheelHouse
   should start automatically when you log in (default: no — but for
   hands-free use, answering yes is strongly recommended), and whether
   to start WheelHouse right now (default: no).

WheelHouse installs for your user only; no administrator rights are
needed. The application, its Python environments, and the speech model
live under `%LOCALAPPDATA%\WheelHouse`. A few small runtime files —
which speech engine is running and on which port — live under
`%APPDATA%\WheelHouse`.

If a download is interrupted, just run the installer again — it resumes
where it left off. Re-running the installer is always safe: it repairs a
broken install and updates an existing one.

### Before the first run

Check that Windows allows desktop apps to use your microphone:
Settings > Privacy and security > Microphone > "Let desktop apps access
your microphone" must be on.

## The speech engine question

The installer asks which speech engine you want. You can change your
answer later by re-running the installer.

| Engine | Where speech is processed | What it needs |
|--------|---------------------------|---------------|
| **Parakeet** (option 1, the default) | On your machine, CPU | Nothing extra. No account, works offline. |
| **Google Cloud** (option 2) | Google's servers | A Google Cloud account and credentials — see the next section. Audio streams to Google while you dictate. |
| **Distil-Whisper** (option 3) | On your machine, NVIDIA graphics card | Offered only when the installer detects an NVIDIA card with at least 4 GB of dedicated memory. Downloads its own model on first start, so the first launch takes a few minutes. |

On an update, pressing Enter keeps the engine you already use. If your
current engine is no longer available on the machine (for example the
NVIDIA card was removed), the installer says so before the question
instead of switching you silently.

## Google Cloud engine: credentials

The Google Cloud engine cannot hear you until it has credentials. This
is the one engine that requires technical setup, and Google charges for
use beyond its free tier.

1. Create a Google Cloud account and a project at
   https://console.cloud.google.com/.
2. In the project, enable the **Cloud Speech-to-Text API** (APIs &
   Services > Enable APIs and services).
3. Create a service account (IAM & Admin > Service Accounts) and give it
   the **Cloud Speech Client** role.
4. Create a key for that service account (Keys > Add key > JSON). A
   `.json` file downloads.
5. Move the file somewhere permanent, for example
   `%LOCALAPPDATA%\WheelHouse\google-credentials.json`.
6. Point Windows at it: press the Windows key, type "environment
   variables", open "Edit environment variables for your account", and
   add a new user variable named `GOOGLE_APPLICATION_CREDENTIALS` whose
   value is the full path to the `.json` file.
7. Restart WheelHouse if it is running.

## Updating

Run the same install command again. The installer recognizes the
existing install, refuses to touch it while WheelHouse is running (exit
WheelHouse first: right-click the tray icon and choose Exit), and
replaces the application while preserving your personal files:

- your settings (`config.toml`),
- your personal voice patterns,
- your approved and declined dictation targets,
- your speech hints,
- the downloaded speech model (it lives outside the application folder).

If an update is interrupted part-way — even by a crash or power loss —
run the installer again. Your preserved files survive in a staging
folder and are restored on the next run.

## Adding or switching engines later

Run the installer again and pick the engine you want at the question.
This is also the fix if you chose an engine earlier but skipped its
model download or setup: for example, picking Parakeet later downloads
the speech model then.

## Security warnings you may see

WheelHouse is currently unsigned — there is no code-signing certificate
yet — so Windows may warn you. The warnings are about the missing
signature, not about what the software does. The entire source code is
public in this repository.

- **SmartScreen** ("Windows protected your PC"): click "More info", then
  "Run anyway".
- **Antivirus flags the download**: some antivirus products rewrite or
  quarantine downloads. If the installer reports that a download "failed
  its integrity check", an antivirus or proxy altering the file is the
  most common cause. Add an exception or install on a different network.
- **A downloaded script will not run**: if you downloaded
  `install-wheelhouse.ps1` as a file instead of using the one-line
  command, Windows marks it as coming from the internet. Either run:
  `Unblock-File .\install-wheelhouse.ps1` first, or start it with
  `powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1`.

If these trade-offs are not acceptable to you, you can read the code and
install from source instead (see [CONTRIBUTING.md](./CONTRIBUTING.md)).

## Uninstall

Uninstalling needs the script as a downloaded file (the one-line command
cannot pass options). Download `install-wheelhouse.ps1` from the same
releases page, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1 -Uninstall
```

The uninstaller refuses to run while WheelHouse is running, asks you to
confirm, and asks whether to keep your personal data. If you keep it,
your settings, voice patterns, and the speech model stay in
`%LOCALAPPDATA%\WheelHouse` (the folder path is printed) so a future
reinstall can use them; copy the files back after reinstalling if you
want them. If you keep nothing, both WheelHouse folders
(`%LOCALAPPDATA%\WheelHouse` and `%APPDATA%\WheelHouse`) and all
shortcuts are removed.

## Troubleshooting

| The installer says | What it means and what to do |
|--------------------|------------------------------|
| "WheelHouse appears to be running" | Exit WheelHouse first: right-click the WheelHouse tray icon and choose Exit (or use the exit voice command). Then run the installer again. |
| "Could not check for a running WheelHouse" or "Could not check whether WheelHouse is currently running" | The Windows check for running programs failed. Early in the install this is only a warning and the install continues. But just before the installer would replace or remove files, it stops instead — it will not risk modifying a copy of WheelHouse that might still be running. Close WheelHouse if it is open, or restart the computer, then run the installer again. |
| "This computer has N GB of memory" | Below the 8 GB minimum for the offline engine. Use the Google Cloud engine instead (see above), or add memory. |
| "Not enough free disk space" | Free up 10 GB on the Windows drive and run the installer again. |
| "tar.exe was not found" / "tar.exe is needed" | Windows 10 before version 1803 does not ship `tar.exe`, which unpacks the speech model. Install tar, or choose the Google Cloud engine. |
| "Could not install uv" | Usually a blocked network. Corporate proxies can block both winget and the uv install script. Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and run the installer again. |
| "Setting up services/... failed" | Setting up a Python environment failed, for one of two reasons the message tells apart. If it says "uv sync exit code N", a package environment could not be built — usually a network or proxy problem; check your connection and run the installer again (it picks up where it left off). If it says the path "is missing or is not a folder", the unpacked files are incomplete or were quarantined — run the installer again to re-download and re-unpack, and check whether antivirus is removing files. |
| "... failed its integrity check" | The downloaded file does not match its published checksum. An antivirus or proxy rewriting downloads is the most common cause; a changed release asset is the other. Try again later or on another network; if it persists, file an issue. |
| "Downloading ... failed twice" | Network trouble. Run the installer again — downloads resume where they left off. |
| "An incomplete speech model was found" | A previous model unpacking was interrupted. This is informational: the installer removes the incomplete files and unpacks the model again from the already-downloaded archive. The 650 MB download does not repeat unless the downloaded file itself is missing or damaged. |
| The speech engine will not start after install | Open a NEW PowerShell window and run `uv --version`. If that fails, uv's folder did not reach your PATH — re-run the installer, which checks and repairs this. |

Anything else: please file an issue at
https://github.com/wheelhouse-project/WheelHouse/issues and paste the installer's
output. Every failure message the installer prints is designed to be
safe to share.
