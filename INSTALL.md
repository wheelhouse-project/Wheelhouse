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

### Running the installer

The installer is a standard Windows setup wizard. Download it and run it -- nothing needs to be installed ahead of time:

https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/Wheelhouse-Setup.exe

If Windows shows a "Windows protected your PC" screen, see [Security warnings](#security-warnings) below. The whole process takes about 10 to 20 minutes, most of it downloading (roughly 1 GB in total). The wizard:

1. Asks its questions up front: which speech engine to use (the pre-selected answer suits most installations -- see [Speech Engines](#speech-engines)), whether to set up the optional AI helper (the wizard offers one AI choice, a cloud model from Google, and skipping; the model that runs on your own machine is set up from the command line instead, described below), and whether Wheelhouse starts when you log in and right after setup finishes (both pre-selected). It also asks you to turn on microphone access for desktop apps, but only when that Windows setting is currently off; when it is already on, setup says nothing about it.
2. Checks the requirements listed under [What you need](#what-you-need). Four of them stop setup when they are not met: 64-bit Windows, the Windows version, free disk space, and the memory floor. In each case setup states on screen what is missing and what to do about it. The rest -- the processor core count and a connected microphone -- produce a notice and setup continues.
3. Installs uv, the environment manager Wheelhouse uses, into the user profile. Nothing is installed system-wide.
4. Downloads the Wheelhouse application, verifies the download against its published fingerprint, and creates Wheelhouse's own Python environments. Those environments are self-contained and separate from any other Python installation on the computer.
5. Downloads the offline speech model if the default engine was kept (650 MB; this is the longest step).
6. Creates Start-menu and desktop shortcuts.
7. Reports anything worth knowing on its final page. Setup can complete and still have had to change something -- installing Parakeet because the graphics card cannot run Distil-Whisper, for example -- and those notices appear there rather than only in the setup log.

Wheelhouse installs for one user account. Administrator rights are not required, and no other program on the computer is modified.

**Command-line installation.** The same install runs as one PowerShell line, asking only the speech-engine, start-at-login (defaults to no), and start-now questions as text prompts. It asks nothing about the AI helper: the AI choice is given as an argument instead, or left out to install without AI. The one-line command cannot carry arguments; to pass one, download install-wheelhouse.ps1 from the release page and run it as a file, for example `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -AiMode local`.

```
irm https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

**Setting up the AI helper on this machine.** Adding `-AiMode local` to the command-line installer sets up an AI model that runs on your own computer, with no account, no key, and no text leaving the machine. Setup measures the hardware before downloading anything: a graphics card of any make with 4 GB or more of video memory runs the model on the card, a machine with 16 GB or more of system memory runs it on the processor instead, which works but is slow -- about 3 seconds for a short correction and about 12 seconds for a long one -- and a machine with less than both is told why and left with the AI features switched off rather than having several gigabytes downloaded that could not run. `-AiMode cloud` selects the Google cloud model the wizard offers, `-AiMode off` installs without AI, and `-AiMode keep` leaves an existing AI configuration alone on a re-run.

### What you need

- Windows 10 or Windows 11, 64-bit. Any Windows 11 edition; most Windows 10 editions.
- 10 GB of free disk space.
- 8 GB of memory (RAM), a hard minimum; 16 GB recommended. Below 8 GB the installer stops and cannot proceed with any speech engine, including the cloud one.
- 4 or more CPU cores recommended. With fewer, Wheelhouse installs and runs, but speech recognition may respond slowly.
- A microphone. One can be connected after installing.
- An internet connection during installation. The default speech engine operates offline afterward.

### Successful installation

The wizard reports its progress step by step, and the PowerShell installer reports the same steps as text. Installation is complete when it reaches the end without stopping on an error. Wheelhouse then appears in the Start menu under W and as a desktop shortcut.

### Installation failure messages

Installer failure messages contain no personal data and can be included in a help request. When the wizard stops, a window states what went wrong and what to try. If the setup log can be found, the same window names the file and in most cases offers to open it; when the log cannot be found, the window omits any reference to a log. Either way it gives an address to write to, help@wheelhouse-project.org. The PowerShell installer prints the same two lines as text. The common messages:

- **"Wheelhouse appears to be running"** (during an update): the installer refuses to replace an application that is running. Exit Wheelhouse first (right-click the floating button or the tray icon -- both open the same menu -- and choose Exit), then run the installer again. If it reports that it could not check, restart the computer and try again.
- **"This computer has N GB of memory"**: your machine is below the 8 GB minimum. This check stops the install for every speech engine, including the cloud one, so adding memory is the only fix.
- **"Not enough free disk space"**: free up 10 GB on the Windows drive and run the installer again.
- **"Unpacking the speech model failed"**: the extraction stopped, and the message includes the extractor's own error text. Run the installer again -- the downloaded archive is kept and the download does not repeat. If it fails the same way twice, include the message in a help request.
- **"Could not install uv"**: usually a blocked network -- corporate proxies can block the download. Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and run the installer again.
- **"... failed its integrity check"**: the downloaded file does not match its published fingerprint. An antivirus or proxy rewriting downloads is the most common cause; a changed release asset is the other. Add an exception or try a different network, and if it keeps failing, file an issue on the GitHub page.
- **"Downloading ... failed twice"**: network trouble. Run the installer again -- downloads resume where they left off.
- **"Setting up services/... failed"**: a Python environment could not be built. If the message shows a "uv sync exit code", it is usually a network or proxy problem -- check the connection and run the installer again. If it says a path "is missing or is not a folder", the unpacked files are incomplete or were quarantined -- run the installer again and check whether antivirus is removing files.
- **"An incomplete speech model was found"**: informational, not an error. A previous unpacking was interrupted; the installer removes the incomplete files and unpacks again from the archive it already has. The 650 MB download only repeats if the archive itself is damaged.
- **No Wheelhouse entry in the Start menu**: check Start > All apps under W first -- new entries are not pinned to the front page. If it is truly absent, the desktop shortcut works the same; the installer log records a "Shortcut created" or "Could not create" line for a help request.

**Re-running the installer is safe at any time.** It repairs a broken install, resumes interrupted downloads, and updates an existing install while preserving your user data; the list of what is preserved is under [Updating Wheelhouse](#updating-wheelhouse).

### Updating Wheelhouse

There is no separate update procedure: **updating is re-running the installer.** Download and run the newest Wheelhouse-Setup.exe, or run the same PowerShell line, from [Running the installer](#running-the-installer). The installer fetches the newest release, and when it finds Wheelhouse already present, it updates it in place. Exit Wheelhouse first -- right-click the floating button or the tray icon, both of which open the same menu, and choose Exit. The installer refuses to replace an application that is running.

An update replaces the application and preserves user data:

- The settings file (config.toml)
- Personal voice patterns
- Approved and declined dictation targets
- Saved speech hints
- The downloaded speech model -- it is stored outside the part an update replaces, so the 650 MB download does not repeat

**If an update is interrupted** -- a power cut, a closed window, a crash -- user files are preserved. Before replacing anything, the installer copies them into a holding folder next to the application, and the next run restores whatever it finds there. Recovery is running the same command again; no manual step is required.

### Security warnings

The Wheelhouse installer is digitally signed by the project's author, David Chesley Hite III, which allows Windows to verify that the download came from the project unaltered. Windows may still warn about each new release until it has seen that file often enough. The source code is public at https://github.com/wheelhouse-project/Wheelhouse.

- **SmartScreen ("Windows protected your PC")**: appears when running a recently released Wheelhouse-Setup.exe. Click "More info", check that the publisher reads David Chesley Hite III, then click "Run anyway". If the setup wizard fails, its failure window names the setup log when it can find the file, and in most cases offers to open it; the file is at `%TEMP%\Setup Log <date> #<number>.txt`. Attach it to a help request.
- **Antivirus flags or rewrites the download**: some antivirus products quarantine downloads or alter them as they arrive. The installer verifies its own downloads -- the application, the speech model, and the AI files -- against published fingerprints and refuses anything altered (the "failed its integrity check" message); uv arrives through winget or uv's own installer and is the one download not checked this way. Add an exception for Wheelhouse, or install on a different network, then run the installer again.
- **A downloaded script will not run**: Windows marks a saved install-wheelhouse.ps1 as coming from the internet, and PowerShell may refuse to run it. Remove the mark once with `Unblock-File .\install-wheelhouse.ps1`, or start it with `powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1`.

Installing from source avoids these warnings. CONTRIBUTING.md in the GitHub repository has the development setup steps.

### Uninstalling Wheelhouse

If you installed with Wheelhouse-Setup.exe, uninstall it like any Windows program: Settings > Apps > Installed apps > Wheelhouse > Uninstall. If you installed with the PowerShell one-liner instead, you need the script as an actual file: download install-wheelhouse.ps1 from the releases page, open PowerShell in that folder, and run:

```
powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall
```

The uninstaller will not run while Wheelhouse is running -- exit it first by right-clicking the floating button or the tray icon and choosing Exit. Run from the command line as above, it asks two questions before removing anything:

1. **"Remove Wheelhouse from this computer?"** -- nothing is removed until this is answered yes.
2. **"Keep your personal data?"** -- the settings file, voice patterns, and the downloaded speech model.

Removed through Windows instead, after a Setup.exe install, only the second question is asked: Windows has already asked whether to uninstall, so the wizard puts the keep-or-remove choice to you and then runs the same uninstaller without repeating the first question.

What each answer does:

- **Keeping personal data:** the application, all its shortcuts, and its bookkeeping folder are removed. The settings file, personal voice patterns, and the speech model remain in `%LOCALAPPDATA%\Wheelhouse`, with the settings and patterns gathered into a subfolder there named preserved-user-data. On a machine where the local AI helper was set up, the AI model and the program that runs it -- several gigabytes -- also remain there. A later reinstall starts from defaults; copy files back from that folder to restore the previous settings and patterns.
- **Keeping nothing:** the entire `%LOCALAPPDATA%\Wheelhouse` folder, the `%APPDATA%\Wheelhouse` folder, and all shortcuts (Start menu, desktop, and the start-at-login entry) are removed. A configured cloud AI access key is also cleared from the user environment.

Those two folders, plus a small `WheelhouseSetup` folder used by the graphical installer's uninstaller, hold everything Wheelhouse itself stores. Setup writes in three further places. It removes two of them: the shortcuts it created and the start-at-login entry. The third it leaves, deliberately -- uv, the environment manager, installed in the user profile, which other programs may also be using. The graphical installer additionally leaves its own log in the Windows temporary folder. `%APPDATA%\Wheelhouse` holds no personal data -- only bookkeeping such as helper-process ID files -- and is removed under either answer. Run from the command line, the uninstaller prints both folder paths when it finishes; removed through Windows, it runs hidden and prints nothing you can see.

### Administrator windows and UAC prompts

Wheelhouse installs for a single user account and runs without administrator rights. A program without administrator rights cannot change system files or settings, and anything it types or clicks is confined to what that user account is permitted to do.

One Windows rule follows from this. Windows does not allow a program to send keystrokes or clicks into a program running as administrator, and applies that rule to all non-administrator software. Two consequences:

- **Programs running as administrator.** A program started with "Run as administrator", or one that elevated itself as some system tools do, cannot receive typed text, key presses, or clicks from Wheelhouse.
- **UAC prompts.** The dimmed "Do you want to allow this app to make changes to your device?" screen is more restricted still: Windows displays it on a separate secure desktop that no ordinary program can reach or observe.

**Observed behavior:** dictation into an administrator window is detected before any keystroke is sent, and a notice appears in the corner of the screen: "Wheelhouse can't type into administrator apps." Nothing is typed. The same notice appears for a terminal running as administrator. Click commands produce their own notice: the contents of a protected window are not visible to Wheelhouse, so "click cancel" reports no match. Spoken key presses such as "press enter" produce no notice -- Windows discards them silently.

**Available options:**

- To dictate into administrator programs, start Wheelhouse as administrator: exit it, right-click its Start menu entry, and choose "Run as administrator".
- Use the physical keyboard and mouse for the administrator window or the UAC prompt, and voice for everything else.
- If the program does not require administrator rights, start it normally. Wheelhouse can then type into it as it does any other program. Some tools require administrator rights and will not run without them; for those, use the two options above.

No Wheelhouse setting removes this limit. Windows enforces it, and the UAC screen remains protected in all cases.

## Speech Engines

### Account requirements

No account is required for the default configuration. Wheelhouse ships with the **Parakeet** engine as its default: it runs on the local processor, works offline, costs nothing, and transmits no audio. The installer downloads its model, and it is preselected in the settings.

An account is required in one case: the **Google Cloud** speech engine, selected at the installer's speech-engine question. That engine processes speech on Google's servers and requires a Google Cloud account and a one-time credentials setup. The account is free and most personal use stays within Google's free tier; Google charges for use beyond it. One limitation: on a computer with less than 8 GB of memory the installer stops before installing anything. Its closing message mentions the cloud engine, but the installer cannot set that engine up on such a machine either, so the remedy is more memory or a different computer.

A third engine, **Distil-Whisper**, runs locally on an NVIDIA graphics card with at least 4 GB of dedicated memory. The two installers differ here. The setup wizard lists it whatever graphics hardware is present; without a suitable card the install sets up Parakeet instead and says so on its final page, among the notices shown there. The command-line installer checks the hardware first and offers Distil-Whisper only when it finds a suitable card. It downloads its own model on first start, so the first launch takes several minutes.

### Local and cloud engines compared

| Aspect | Local engines (Parakeet, Distil-Whisper) | Cloud engine (Google Cloud) |
|---|---|---|
| Accuracy | Very good for everyday dictation and commands | Very good; may have an edge on unusual names and vocabulary |
| Latency | Depends on your computer's speed; about 1.5-2 seconds to the first word on modern hardware | Depends on your internet connection, not your computer |
| Privacy | Audio never leaves your machine | Audio streams to Google's servers while you dictate |
| Cost | Free | Free tier, then Google charges for use beyond it |
| Account needed | None | A Google Cloud account and a one-time credentials setup |
| Works offline | Yes | No |

### Setting up Google Cloud credentials (only if you chose that engine)

This section applies only if you selected the **Google Cloud** speech engine at the installer's speech-engine question. With the default Parakeet engine, skip it: that engine requires no account and no credentials.

If you selected Google Cloud, the installer ended with a warning that the engine requires credentials before it can transcribe, and referred you to "the Google Cloud section". This is that section.

1. Create a Google Cloud account and a project at https://console.cloud.google.com/.
2. In the project, enable the Cloud Speech-to-Text API.
3. Create a service account (under IAM & Admin > Service Accounts) and give it the Cloud Speech Client role.
4. Create a JSON key for that service account; a small file downloads.
5. Move the file somewhere permanent on your computer.
6. Right-click either the floating button or the tray icon -- both open the same menu -- choose **Google Cloud Credentials**, and select the file. Wheelhouse checks that the file is a service-account key, saves its location in the settings, and restarts the Google engine if it is the one running, so the key takes effect without a restart.

The older method still works as an alternative: set an environment variable named GOOGLE_APPLICATION_CREDENTIALS to the full path of the file (press the Windows key, type "environment variables", open "Edit environment variables for your account"), then restart Wheelhouse. Google's own software reads that variable automatically; Wheelhouse uses it whenever no file has been chosen from the menu.

### Adding or switching engines later

To switch between engines already set up on this computer, right-click either the floating button or the tray icon -- both open the same menu -- open **STT Provider**, and select the engine. The change takes effect at once: Wheelhouse stops the running engine, starts the one you chose, and then records it as last_provider in the stt section of the settings file so the next start comes back on it. If the new engine fails to start, the choice is not recorded and the next start returns to the previous engine. Switching to Google Cloud this way does not set up its credentials; see the Google Cloud section above.

To add an engine that was never set up on this machine, re-run the installer and select that engine at its speech-engine question. The installer downloads and sets up what that engine requires, except that Distil-Whisper's model is downloaded by the engine itself the first time it starts. For example, moving from Google Cloud to Parakeet requires the re-run, because that is what downloads Parakeet's speech model; selecting it from the menu alone is not sufficient. Distil-Whisper is always added this way, since the installer sets it up only when it is selected.

The same re-run repairs a missing or incomplete speech model, for example after an interrupted download. The installer detects an incomplete model and reinstalls it. Re-running the installer is safe at any time, and the speech-engine question defaults to the engine already installed, so pressing Enter keeps it. If the current engine is no longer available on this hardware, the PowerShell installer reports that before asking; the setup wizard does not.

### Installer troubleshooting

**Installer failures**

Each installer failure message, and the action for it, is listed under [Installation failure messages](#installation-failure-messages). Re-running the installer is safe, interrupted downloads resume, and the messages contain no personal data and can be included in a help request.

---

Need help with something this guide does not cover? Open an issue at
https://github.com/wheelhouse-project/Wheelhouse/issues and paste the
installer's output -- every message the installer prints is designed to
be safe to share.
