# Installing WheelHouse

<!-- GENERATED FILE -- do not edit by hand. This file is extracted from
     the WheelHouse help document
     (services/wheelhouse/knowledge/wheelhouse_help.md) by
     scripts/release/extract_install_md.py in the private repository.
     Edit the help document and re-run the extractor; a release test
     keeps this file in sync. -->

> This guide is extracted from the [WheelHouse help
> document](services/wheelhouse/knowledge/wheelhouse_help.md), the
> project's source of truth for using and installing WheelHouse. It
> covers what you need, installing, updating, switching speech engines,
> security warnings you may see, and uninstalling.

### Installation

You do not need to install anything ahead of time -- no programming tools, no separate downloads. One command does the whole job. Open any PowerShell window (press the Windows key, type "powershell", press Enter) and run:

```
irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

The whole process takes about 10 to 20 minutes, most of it downloading (roughly 1 GB in total). In plain language, the installer:

1. Checks that your computer meets the requirements (see below) and tells you clearly if something is missing.
2. Installs uv, the environment manager WheelHouse uses, into your user profile and adds it to your user PATH -- nothing system-wide. The Python environments uv builds live inside WheelHouse's own folder.
3. Downloads the WheelHouse application and verifies the download is genuine and undamaged.
4. Sets up WheelHouse's own private Python environments -- these are self-contained and cannot interfere with anything else on your computer.
5. Asks which speech engine you want (the default answer is right for almost everyone -- see Speech Engines and Accounts below).
6. Downloads the offline speech model if you chose the default engine (about 650 MB -- this is the longest step).
7. Creates Start-menu and desktop shortcuts, then asks two final questions: whether WheelHouse should start automatically when you log in (for hands-free use, answering yes is strongly recommended), and whether to start WheelHouse right now.

WheelHouse installs for your user account only. No administrator rights are needed, and it does not touch other programs on your computer.

### What you need

- Windows 10 or 11, 64-bit (Windows 11 any edition; most Windows 10 editions work too)
- 10 GB of free disk space
- 8 GB of memory (RAM) -- a hard minimum; 16 GB is recommended. Below 8 GB the installer stops and cannot proceed with any speech engine, including the cloud one.
- 4 or more CPU cores recommended -- with fewer, WheelHouse still installs, but speech recognition may respond slowly
- A microphone (you can plug one in after installing)
- An internet connection for the install itself; the default speech engine works fully offline after that

### What successful installation looks like

The installer reports each step as it goes. If it reached the speech-engine question, finished its downloads, created your shortcuts, and asked the two final questions (start at login? start now?) without stopping on an error, you are done. You will find WheelHouse in the Start menu under W and as a desktop shortcut.

### What failure looks like

Every failure message the installer prints is designed to be understandable and safe to share. The common ones:

- **"WheelHouse appears to be running"** (during an update): the installer refuses to replace an app that is running. Exit WheelHouse first -- right-click the WheelHouse tray icon and choose Exit -- then run the installer again. If it says it could not even check whether WheelHouse is running, close WheelHouse or restart the computer, then try again.
- **"This computer has N GB of memory"**: your machine is below the 8 GB minimum. This check stops the install for every speech engine, including the cloud one, so adding memory is the only fix.
- **"Not enough free disk space"**: free up 10 GB on the Windows drive and run the installer again.
- **"tar.exe was not found"**: only affects Windows 10 versions from before 2018, which lack the tool that unpacks the speech model. Install tar yourself, or choose the Google Cloud engine (which needs no model download).
- **"Could not install uv"**: usually a blocked network -- corporate proxies can block the download. Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and run the installer again.
- **"... failed its integrity check"**: the downloaded file does not match its published fingerprint. An antivirus or proxy rewriting downloads is the most common cause; a changed release asset is the other. Add an exception or try a different network, and if it keeps failing, file an issue on the GitHub page.
- **"Downloading ... failed twice"**: network trouble. Run the installer again -- downloads resume where they left off.
- **"Setting up services/... failed"**: a Python environment could not be built. If the message shows a "uv sync exit code", it is usually a network or proxy problem -- check the connection and run the installer again (it picks up where it left off). If it says a path "is missing or is not a folder", the unpacked files are incomplete or were quarantined -- run the installer again, and check whether antivirus is removing files.
- **"An incomplete speech model was found"**: informational, not an error. A previous unpacking was interrupted; the installer removes the incomplete files and unpacks again from the archive it already has. The 650 MB download does not repeat unless the downloaded file itself is damaged.
- **No WheelHouse entry in the Start menu**: check Start > All apps under W first -- new entries are not pinned to the front page. If it is truly absent, the desktop shortcut works the same, and the installer's log records a "Shortcut created" or "Could not create" line you can paste into a help request.

**Re-running the installer is always safe.** It repairs a broken install, resumes interrupted downloads, and updates an existing install while preserving your settings, your personal voice patterns, your approved dictation targets, your saved speech hints, and the downloaded speech model. You cannot make things worse by running it again -- when in doubt, re-run it.

### Updating WheelHouse

There is no separate update procedure: **updating IS re-running the installer.** Run the same one-line command from the Installation section. The command always fetches the newest release, and when the installer finds WheelHouse already on your computer, it updates it in place. Exit WheelHouse first (right-click the WheelHouse tray icon and choose Exit) -- the installer refuses to replace an app that is running.

An update replaces the application but keeps everything that is yours:

- Your settings (the config.toml file)
- Your personal voice patterns
- The dictation targets you have approved or declined
- Your saved speech hints
- The downloaded speech model -- it is stored outside the part an update replaces, so the roughly 650 MB download does not repeat

**If an update is interrupted** -- a power cut, a closed window, a crash -- your personal files are safe. Before replacing anything, the installer copies them into a holding folder next to the application, and the next run restores whatever it finds there. Recovery is simply running the same command again; nothing manual is needed.

### Security warnings you may see

The WheelHouse installer is digitally signed by the project's author, David Chesley Hite III, so Windows can verify the download came from the project unaltered. Windows may still warn you for a while after each new release, until it has seen the new file often enough. The complete source code is public at https://github.com/wheelhouse-project/WheelHouse, so anyone can inspect exactly what it does.

- **SmartScreen ("Windows protected your PC")**: can appear when you run a freshly released WheelHouse-Setup.exe. Click "More info", check that the publisher reads David Chesley Hite III, then click "Run anyway". If the setup wizard later runs into trouble, it always writes a log file at `%TEMP%\Setup Log <date> #<number>.txt` -- paste that into a help request.
- **Antivirus flags or rewrites the download**: some antivirus products quarantine downloads or alter them as they arrive. The installer checks every file it downloads against a published fingerprint and refuses anything that does not match, so a changed file cannot be installed -- instead you see a message saying a download "failed its integrity check". An antivirus or proxy altering the file is the most common cause. Add an exception for WheelHouse, or install on a different network, then run the installer again.
- **A downloaded script will not run**: if you saved install-wheelhouse.ps1 as a file (for example, to uninstall) instead of using the one-line command, Windows marks the file as coming from the internet and PowerShell may refuse to run it. Two equally good fixes: remove the mark once with `Unblock-File .\install-wheelhouse.ps1`, or start it with `powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1`.

If you would rather not click through security warnings, read the code and install from source: CONTRIBUTING.md in the GitHub repository has the development setup steps.

### Uninstalling WheelHouse

The installer is also the uninstaller. The one-line install command cannot pass options, so uninstalling needs the script as an actual file: download the same install-wheelhouse.ps1 from the releases page, open PowerShell in the folder where you saved it, and run:

```
powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall
```

The uninstaller will not run while WheelHouse is running -- exit it first (right-click the WheelHouse tray icon and choose Exit). It then asks two questions before touching anything:

1. **"Remove WheelHouse from this computer?"** -- nothing is removed until you type yes.
2. **"Keep your personal data?"** -- meaning your settings, your voice patterns, and the downloaded speech model.

What each answer does:

- **If you keep your personal data:** the application, all its shortcuts, and its technical bookkeeping folder are removed, but your settings, your personal voice patterns, and the speech model stay behind in `%LOCALAPPDATA%\WheelHouse` (the settings and patterns are gathered into a subfolder there named preserved-user-data). If you reinstall later, the installer starts fresh -- copy files back from that folder if you want your old settings and patterns again.
- **If you keep nothing:** everything is removed -- the entire `%LOCALAPPDATA%\WheelHouse` folder and the `%APPDATA%\WheelHouse` folder, plus all shortcuts (Start menu, desktop, and the start-at-login entry). If you had set up a cloud AI access key, the uninstaller also clears it from your user environment.

For the privacy-minded: those two folders are the only places WheelHouse lives, and `%APPDATA%\WheelHouse` never holds personal data (only technical bookkeeping such as helper-process ID files) -- it is removed either way. When the uninstaller finishes, it prints both folder paths so you can check for leftovers yourself.

### When WheelHouse cannot type: administrator windows and UAC prompts

WheelHouse installs for your user account only and runs without administrator rights. That is deliberate, and it is good for your safety: a program with no administrator power cannot change system files or settings, and nothing it types or clicks on your behalf can go further than your own account is allowed to go.

The trade-off is one Windows rule you will occasionally run into. Windows does not allow a normal program to send keystrokes or clicks into a program that is running as administrator. This is a protection built into Windows itself -- it stops any non-administrator software, not just WheelHouse, from pressing buttons in privileged windows. In practice it means two things:

- **Programs running as administrator.** If you started a program with "Run as administrator" (or it elevated itself, as some system tools do), WheelHouse cannot type into it, press keys in it, or click its buttons.
- **UAC prompts.** The dimmed "Do you want to allow this app to make changes to your device?" screen is even more protected: Windows shows it on a separate secure desktop that no ordinary program can reach or even see.

**What it looks like:** for dictation and keystroke commands, nothing -- you speak and the words simply have no effect in that window, with no error message. Click commands do show a notice: WheelHouse cannot see inside the protected window, so "click cancel" reports no match. If WheelHouse suddenly seems to have stopped working, check whether the window you are in is running as administrator. Click into any normal window (Notepad, your browser) and WheelHouse works there immediately, because WheelHouse itself never stopped -- only that one window was out of reach.

**What to do:**

- Use your physical keyboard and mouse for the administrator window or the UAC prompt, then go back to voice for everything else.
- If the program does not actually need administrator rights, start it the normal way (without "Run as administrator"). WheelHouse can then type into it like any other program. Some tools genuinely require administrator rights and will not run unelevated -- for those, keyboard and mouse are the answer.

No WheelHouse setting can lift this limit. It is enforced by Windows, not by WheelHouse, and it protects you against far worse than a missed dictation.

## Speech Engines and Accounts

### Do I need a Google account? (Short answer: probably not)

Most users need no account of any kind. WheelHouse ships with the **Parakeet** engine as its default: it runs entirely on your own computer, on the regular processor (CPU), works offline, costs nothing, and never sends your audio anywhere. The installer downloads its model for you, and it is preselected in your settings from the start.

The one situation where an account comes up: you picked the **Google Cloud** speech engine at the installer's speech-engine question. That engine processes your speech on Google's servers and needs a free Google Cloud account plus a one-time credentials setup (Google charges for heavy use beyond its free tier, but most personal use stays within it). One caveat: on a computer with less than 8 GB of memory, the installer stops before installing anything -- its closing message mentions the cloud engine, but the installer cannot yet set it up on such a machine, so the fix is adding memory or using another computer.

There is also a third option for computers with an NVIDIA graphics card that has at least 4 GB of dedicated memory: **Distil-Whisper**, which runs locally on the graphics card. The installer offers it only when it detects a suitable card. It downloads its own model the first time it starts, so the first launch takes a few minutes.

### Local versus cloud, honestly compared

| Aspect | Local engines (Parakeet, Distil-Whisper) | Cloud engine (Google Cloud) |
|---|---|---|
| Accuracy | Very good for everyday dictation and commands | Very good; may have an edge on unusual names and vocabulary |
| Latency | Depends on your computer's speed; about 1.5-2 seconds to the first word on modern hardware | Depends on your internet connection, not your computer |
| Privacy | Audio never leaves your machine | Audio streams to Google's servers while you dictate |
| Cost | Free | Free tier, then Google charges for use beyond it |
| Account needed | None | A Google Cloud account and a one-time credentials setup |
| Works offline | Yes | No |

### Setting up Google Cloud credentials (only if you chose that engine)

You need this section only if you picked the **Google Cloud** speech engine at the installer's speech-engine question. If you use the default Parakeet engine, skip this section entirely: it needs no account and no credentials.

If you chose Google Cloud, the installer ended with a warning that the engine needs credentials before it can hear you, and pointed you to "the Google Cloud section" -- this is that section. The account itself is free, and most personal use stays within Google's free tier (Google charges for heavy use beyond it).

1. Create a Google Cloud account and a project at https://console.cloud.google.com/.
2. In the project, enable the Cloud Speech-to-Text API.
3. Create a service account (under IAM & Admin > Service Accounts) and give it the Cloud Speech Client role.
4. Create a JSON key for that service account; a small file downloads.
5. Move the file somewhere permanent on your computer.
6. Press the Windows key, type "environment variables", open "Edit environment variables for your account", and add a new variable named GOOGLE_APPLICATION_CREDENTIALS whose value is the full path to that file.
7. Restart WheelHouse if it is running.

That GOOGLE_APPLICATION_CREDENTIALS variable is where WheelHouse expects to find the file: Google's own software reads it automatically, so there is nothing to edit inside WheelHouse itself.

### Adding or switching engines later

To switch between engines that are already set up on this computer, use the system tray: right-click the WheelHouse icon, open **STT Provider**, and pick the engine you want. WheelHouse remembers your choice (it is stored as last_provider in the stt section of the settings file) and uses it the next time it starts. If you switch to Google Cloud this way, remember that it cannot hear you until its credentials are set up -- see the Google Cloud section above.

To add an engine that was never set up on this machine, re-run the installer and pick that engine at its speech-engine question; it downloads and sets up whatever the engine needs. For example, if you originally chose Google Cloud and now want Parakeet, the re-run is what downloads Parakeet's speech model -- picking it from the tray menu is not enough on its own. The Distil-Whisper engine is always added this way: the installer sets it up only when you choose it, and offers it only on a computer with a suitable NVIDIA graphics card.

The same re-run is the repair path when the speech model is missing or incomplete -- for example when its download was skipped or interrupted the first time. The installer notices an incomplete model and reinstalls it. Re-running the installer is always safe, and on a re-run the speech-engine question defaults to the engine you already have, so pressing Enter keeps it (if your current engine is no longer available on this computer's hardware, the installer says so before asking).

### Installer troubleshooting

**Installer failures**

The installer's failure messages -- low memory, low disk space, a blocked uv download, an integrity-check failure, an interrupted download, a failed services setup, an incomplete speech model, or WheelHouse still running during an update -- are explained in the "What failure looks like" part of the Getting Started section, along with what to do about each. The short version: re-running the installer is always safe, downloads resume where they left off, and every message is safe to paste into a help request.

---

Need help with something this guide does not cover? Open an issue at
https://github.com/wheelhouse-project/WheelHouse/issues and paste the
installer's output -- every message the installer prints is designed to
be safe to share.
