# Wheelhouse Help Document

## Instructions for AI Assistant

You are a friendly, patient Wheelhouse support assistant. You help two kinds
of people: current users of Wheelhouse, and people who have not installed it
yet and are deciding whether to try it. Wheelhouse is a voice-controlled
desktop automation system for Windows.

Behavior rules:
- Match your depth to the question. Simple question = simple answer. Technical
  question = technical answer.
- If the user seems non-technical, avoid jargon. Use analogies.
- If unsure whether the user wants a quick or detailed answer, ask:
  "Would you like a quick answer or a deeper explanation?"
- For someone deciding whether to install: answer accurately from the
  "Overview", "System Requirements", "Speech Engines", and "Installation and
  Setup" sections. Be candid about hardware limits and rough edges. Never
  oversell.
- For Wheelhouse-specific questions: answer only from the Wheelhouse documents
  provided to you -- this help document and, when it is provided, the separate
  Wheelhouse command and configuration reference. Never invent features,
  commands, or settings that none of the provided documents describe. For the
  exact wording of a voice command, or a configuration setting and its default
  value, use the command and configuration reference when it is available: this
  help document explains the features in prose, but the complete list of every
  command and setting now lives in that reference.
- For general computing questions (microphone setup, Windows settings,
  PowerShell basics): help freely using your general knowledge.
- If the answer isn't in any of the documents provided to you: "I don't have
  information about that feature. You can reach the developer at the Wheelhouse
  GitHub page: https://github.com/wheelhouse-project/Wheelhouse (open an issue
  or start a discussion)."
- This document contains HTML comment lines such as <!-- install-doc:start -->.
  They are structural markers for tooling. Ignore them and never mention them.
- If an answer could depend on the Wheelhouse version (behavior that changed,
  download sizes, feature availability), tell the user which release this
  document describes -- read it from the "Generated" line in the footer at
  the very end ("for the vX.Y.Z release"). Ignore the footer's "Wheelhouse
  version" line; it is an internal build identifier. The separate command and
  configuration reference names its own release in its own "Generated" footer
  line the same way.
- When describing voice commands, always give an example of what to say.
- When a user seems overwhelmed, direct them to the "Quick Start" section
  and tell them to ignore everything else until they're comfortable.
- When a user asks about hardware or performance, be direct about limitations.
  Don't promise it will work on every machine.
- Greet the user and ask what they need help with.

---

## Overview

Wheelhouse controls a Windows PC by voice. It performs five functions: dictating text into any application, executing spoken commands, switching windows, launching programs, and clicking on-screen controls by name. None of them require the keyboard or mouse. On a Logitech MX-series mouse, Wheelhouse can additionally assign screen brightness and volume to the thumb wheel.

**Intended use.** Wheelhouse is a general-purpose voice interface and an assistive technology. A user for whom a keyboard and mouse are painful, difficult or impossible can operate the computer through the voice interface alone.

**Requirements.**

- A Windows 10 or Windows 11 PC (64-bit)
- A microphone. A laptop's built-in microphone is usually adequate. If recognition accuracy is poor, a headset or external microphone is worth trying, and one can be connected after installing.
- About 10 GB of free disk space. Most of that is the Python environments the program and its speech engines run in; the speech model itself is the smaller part.

The installer provides everything else and checks the hardware before it begins. No account, subscription or prior installation of other software is required. The full requirements, including memory and processor, are in [System Requirements](#system-requirements) below.

**Where speech is processed.** By default, Wheelhouse converts speech to text on the local machine. No audio or text is transmitted, and the application reports no telemetry. Cloud speech recognition is available; it is not the default, and it can be selected at install time or afterwards. [Speech Engines](#speech-engines) compares the engines and states what each one transmits.

**Operation.** Wheelhouse converts microphone audio to text on the local machine and then classifies the result. Text matching a known command ("undo", "select all") is executed as that command. All other text is dictation, and is inserted into the focused window -- a document, an email, a chat field -- with capitalization and spacing applied automatically. Punctuation is spoken: "comma" and "period" insert the corresponding symbols. Text is inserted continuously while you speak, typically beginning within two seconds, rather than after the utterance ends. [Speech Modes](#speech-modes) documents how the classification is decided.

**Getting answers.** The Wheelhouse Assistant answers questions about any part of this document in plain language, without requiring you to find the right section first. It also holds the full command and configuration reference, so it answers questions this document does not cover. See [Getting Help](#getting-help).

**Project status.** Wheelhouse is an open-source project with a single primary author. It is in daily use by the author, but it has been tested on a limited set of machines, so defects on untested hardware and in untested applications are expected. Report failures at https://github.com/wheelhouse-project/Wheelhouse

---

## Quick Start

Complete these steps before reading the rest of this document. They verify that the installation works.

1. Download the installer and run it: https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/Wheelhouse-Setup.exe
   If Windows shows "Windows protected your PC", click "More info", check that the publisher reads David Chesley Hite III, and click "Run anyway". The setup wizard's pre-selected answers are right for almost everyone. It downloads the speech model, so give it 10 to 20 minutes.
2. Start Wheelhouse from the Start menu or the desktop shortcut (the installer creates both).
3. Open Notepad.
4. Say **"hello world"** -- the words "hello world" appear.
5. Say **"new line"** -- the cursor moves to a new line.
6. Say **"undo"** -- the text is undone.
7. Say **"select all"** -- the text is highlighted.

If steps 4 to 7 produce the results described, the installation is working.

Wheelhouse starts in click-to-talk (toggle) mode and starts listening, which is why steps 4 to 7 need no click. One click on the floating button or the tray icon switches listening off, and another switches it back on. To hold a button down while speaking instead, see [Interaction Modes](#interaction-modes) below.

If a step does not behave as described, see [Setup verification](#setup-verification) in Troubleshooting, or ask the Wheelhouse Assistant, which answers questions about any part of this document ([Getting Help](#getting-help)).

---

## Where to Go Next

Once the quick start works, continue with the section that matches the task.

- **Dictating text into email, documents and chat:** [Voice Commands](#voice-commands), in particular the dictation and punctuation subsections, then [Speech Modes](#speech-modes).
- **Using the full command set:** the complete [Voice Commands](#voice-commands) reference, covering commands, formatting and navigation, then [Configuration](#configuration). Every shipped command and every setting is also listed in the [command and configuration reference](https://wheelhouse-project.org/reference.html).
- **Installing, configuring or diagnosing a fault:** [Installation and Setup](#installation-and-setup), then [Configuration](#configuration), then [Troubleshooting](#troubleshooting). For a question that none of those answer directly, the Wheelhouse Assistant answers from this document in plain language; see [Getting Help](#getting-help).

---

## Installation and Setup

Installing Wheelhouse, updating it, removing it, and the checks to perform after the first start.

<!-- install-doc:start -->

### Running the installer

The installer is a standard Windows setup wizard. Download it and run it -- nothing needs to be installed ahead of time:

https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/Wheelhouse-Setup.exe

If Windows shows a "Windows protected your PC" screen, see [Security warnings](#security-warnings) below. The whole process takes about 10 to 20 minutes, most of it downloading (roughly 1 GB in total). The wizard:

1. Asks its questions up front: which speech engine to use (the pre-selected answer suits most installations -- see [Speech Engines](#speech-engines)), whether to set up the optional AI helper (the wizard offers one AI choice, a cloud model from Google, and skipping; the model that runs on your own machine is set up from the command line instead, described below), and whether Wheelhouse starts when you log in and right after setup finishes (both pre-selected). It also asks you to turn on microphone access for desktop apps, but only when that Windows setting is currently off; when it is already on, setup says nothing about it.
2. Checks the requirements listed under [What you need](#what-you-need). Four of them stop setup when they are not met: 64-bit Windows, the Windows version, free disk space, and the memory floor. In each case setup states on screen what is missing and what to do about it. The rest -- the processor core count, a connected microphone, and the Windows tool that unpacks the speech model -- produce a notice and setup continues.
3. Installs uv, the environment manager Wheelhouse uses, into the user profile. Nothing is installed system-wide.
4. Downloads the Wheelhouse application, verifies the download against its published fingerprint, and creates Wheelhouse's own Python environments. Those environments are self-contained and separate from any other Python installation on the computer.
5. Downloads the offline speech model if the default engine was kept (650 MB; this is the longest step).
6. Creates Start-menu and desktop shortcuts.
7. Reports anything worth knowing on its final page. Setup can complete and still have had to change something -- installing Parakeet because the graphics card cannot run Distil-Whisper, for example -- and those notices appear there rather than only in the setup log.

Wheelhouse installs for one user account. Administrator rights are not required, and no other program on the computer is modified.

**Command-line installation.** The same install runs as one PowerShell line, asking only the speech-engine, start-at-login (defaults to no), and start-now questions as text prompts. It asks nothing about the AI helper: the AI choice is given as an argument instead, or left out to install without AI.

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
- **"tar.exe was not found"**: only affects Windows 10 versions from before 2018, which lack the tool that unpacks the speech model. Install tar yourself, or choose the Google Cloud engine (which needs no model download).
- **"Could not install uv"**: usually a blocked network -- corporate proxies can block the download. Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and run the installer again.
- **"... failed its integrity check"**: the downloaded file does not match its published fingerprint. An antivirus or proxy rewriting downloads is the most common cause; a changed release asset is the other. Add an exception or try a different network, and if it keeps failing, file an issue on the GitHub page.
- **"Downloading ... failed twice"**: network trouble. Run the installer again -- downloads resume where they left off.
- **"Setting up services/... failed"**: a Python environment could not be built. If the message shows a "uv sync exit code", it is usually a network or proxy problem -- check the connection and run the installer again. If it says a path "is missing or is not a folder", the unpacked files are incomplete or were quarantined -- run the installer again and check whether antivirus is removing files.
- **"An incomplete speech model was found"**: informational, not an error. A previous unpacking was interrupted; the installer removes the incomplete files and unpacks again from the archive it already has. The 650 MB download only repeats if the archive itself is damaged.
- **No Wheelhouse entry in the Start menu**: check Start > All apps under W first -- new entries are not pinned to the front page. If it is truly absent, the desktop shortcut works the same; the installer log records a "Shortcut created" or "Could not create" line for a help request.

**Re-running the installer is safe at any time.** It repairs a broken install, resumes interrupted downloads, and updates an existing install while preserving your user data; the list of what is preserved is under [Updating Wheelhouse](#updating-wheelhouse).

<!-- install-doc:end -->

If none of these apply, the Wheelhouse Assistant can read an installer message and identify the cause; see [Getting Help](#getting-help). Reports can also be filed at https://github.com/wheelhouse-project/Wheelhouse or sent to help@wheelhouse-project.org -- include the installer's output, or attach the setup log.

<!-- install-doc:start -->

### Updating Wheelhouse

There is no separate update procedure: **updating is re-running the installer.** Download and run the newest Wheelhouse-Setup.exe, or run the same PowerShell line, from [Running the installer](#running-the-installer). The installer fetches the newest release, and when it finds Wheelhouse already present, it updates it in place. Exit Wheelhouse first -- right-click the floating button or the tray icon, both of which open the same menu, and choose Exit. The installer refuses to replace an application that is running.

An update replaces the application and preserves user data:

- The settings file (config.toml)
- Personal voice patterns
- Approved and declined dictation targets
- Saved speech hints
- The downloaded speech model -- it is stored outside the part an update replaces, so the 650 MB download does not repeat

**If an update is interrupted** -- a power cut, a closed window, a crash -- user files are preserved. Before replacing anything, the installer copies them into a holding folder next to the application, and the next run restores whatever it finds there. Recovery is running the same command again; no manual step is required.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Security warnings

The Wheelhouse installer is digitally signed by the project's author, David Chesley Hite III, which allows Windows to verify that the download came from the project unaltered. Windows may still warn about each new release until it has seen that file often enough. The source code is public at https://github.com/wheelhouse-project/Wheelhouse.

- **SmartScreen ("Windows protected your PC")**: appears when running a recently released Wheelhouse-Setup.exe. Click "More info", check that the publisher reads David Chesley Hite III, then click "Run anyway". If the setup wizard fails, its failure window names the setup log when it can find the file, and in most cases offers to open it; the file is at `%TEMP%\Setup Log <date> #<number>.txt`. Attach it to a help request.
- **Antivirus flags or rewrites the download**: some antivirus products quarantine downloads or alter them as they arrive. The installer verifies every download against a published fingerprint and refuses anything altered (the "failed its integrity check" message). Add an exception for Wheelhouse, or install on a different network, then run the installer again.
- **A downloaded script will not run**: Windows marks a saved install-wheelhouse.ps1 as coming from the internet, and PowerShell may refuse to run it. Remove the mark once with `Unblock-File .\install-wheelhouse.ps1`, or start it with `powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1`.

Installing from source avoids these warnings. CONTRIBUTING.md in the GitHub repository has the development setup steps.

<!-- install-doc:end -->

<!-- install-doc:start -->

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

- **Keeping personal data:** the application, all its shortcuts, and its bookkeeping folder are removed. The settings file, personal voice patterns, and the speech model remain in `%LOCALAPPDATA%\Wheelhouse`, with the settings and patterns gathered into a subfolder there named preserved-user-data. A later reinstall starts from defaults; copy files back from that folder to restore the previous settings and patterns.
- **Keeping nothing:** the entire `%LOCALAPPDATA%\Wheelhouse` folder, the `%APPDATA%\Wheelhouse` folder, and all shortcuts (Start menu, desktop, and the start-at-login entry) are removed. A configured cloud AI access key is also cleared from the user environment.

Those two folders, plus a small `WheelhouseSetup` folder used by the graphical installer's uninstaller, hold everything Wheelhouse itself stores. Setup writes in three further places. It removes two of them: the shortcuts it created and the start-at-login entry. The third it leaves, deliberately -- uv, the environment manager, installed in the user profile, which other programs may also be using. The graphical installer additionally leaves its own log in the Windows temporary folder. `%APPDATA%\Wheelhouse` holds no personal data -- only bookkeeping such as helper-process ID files -- and is removed under either answer. Run from the command line, the uninstaller prints both folder paths when it finishes; removed through Windows, it runs hidden and prints nothing you can see.

<!-- install-doc:end -->

<!-- install-doc:start -->

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

<!-- install-doc:end -->

### First run

Starting Wheelhouse starts five programs together: **the launcher** (the program started from the shortcut; it supervises the others and restarts any that crash), **the logic process** (interprets recognized speech and routes it to an action), **the input process** (types text, presses keys, and performs clicks), **the GUI process** (the tray icon and the floating status button), and **the speech engine** (a separate helper program that converts audio to text). Within a few seconds the Wheelhouse icon appears in the system tray, the area near the clock. If it does not appear, see [Troubleshooting](#troubleshooting).

### Microphone verification

Confirm that Windows itself receives audio from the microphone before diagnosing recognition problems. Three checks, in order:

1. **The privacy setting.** Open Settings > Privacy and security > Microphone and confirm that "Let desktop apps access your microphone" is on. With this setting off, no audio reaches Wheelhouse and no error is reported.
2. **The input meter.** Right-click the taskbar speaker icon, choose Sound settings, and scroll to Input. The intended microphone should be selected, and the level meter should move while you speak. If the meter stays flat, select a different input device.
3. **A dictation test.** Open Notepad, confirm that Wheelhouse is listening, and say "hello world". On current hardware the words appear within about two seconds.

### The hotword ("x-ray")

Some commands would have destructive effects if they fired during dictation -- closing a window, for example. Those commands run only when the utterance begins with the word "x-ray". "close window" is transcribed as ordinary dictation; "x-ray close window" closes the active window. Common commands such as "undo", "copy", and "select all" do not require the hotword. Throughout this document a command that requires it is written with the "x-ray" prefix and one that does not is written without it; the command reference states the requirement for every command individually.

### The wake word ("computer")

After a period with no keyboard or mouse activity, Wheelhouse pauses listening -- the measure is input to the computer, not silence, so a film watched without touching either will trigger the pause. Saying "computer" resumes it, without keyboard or mouse. The wake word and the hotword serve different purposes: "computer" resumes listening after an idle pause, and "x-ray" runs a protected command. Wake-word behavior is configurable in the wake_word section of the settings file; it is enabled by default.

---

<!-- install-doc:start -->

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

<!-- install-doc:end -->

<!-- install-doc:start -->

### Adding or switching engines later

To switch between engines already set up on this computer, right-click either the floating button or the tray icon -- both open the same menu -- open **STT Provider**, and select the engine. The change takes effect at once: Wheelhouse stops the running engine, starts the one you chose, and then records it as last_provider in the stt section of the settings file so the next start comes back on it. If the new engine fails to start, the choice is not recorded and the next start returns to the previous engine. Switching to Google Cloud this way does not set up its credentials; see the Google Cloud section above.

To add an engine that was never set up on this machine, re-run the installer and select that engine at its speech-engine question. The installer downloads and sets up whatever that engine requires. For example, moving from Google Cloud to Parakeet requires the re-run, because that is what downloads Parakeet's speech model; selecting it from the menu alone is not sufficient. Distil-Whisper is always added this way, since the installer sets it up only when it is selected.

The same re-run repairs a missing or incomplete speech model, for example after an interrupted download. The installer detects an incomplete model and reinstalls it. Re-running the installer is safe at any time, and the speech-engine question defaults to the engine already installed, so pressing Enter keeps it. If the current engine is no longer available on this hardware, the PowerShell installer reports that before asking; the setup wizard does not.

<!-- install-doc:end -->

---

## System Requirements

The hardware Wheelhouse requires, the hardware it performs well on, and the response times to expect between the two.

### Minimum requirements

- Windows 10 or Windows 11, 64-bit
- A dual-core processor -- Wheelhouse will install and run, but speech recognition may respond slowly; 4 or more cores is the comfortable floor
- 8 GB of RAM -- a hard minimum; below it, the installer stops and cannot proceed with any speech engine, including the cloud one
- 10 GB of free disk space
- An SSD is strongly recommended -- on an old spinning hard drive, startup and first responses are noticeably slower
- A working microphone

### Recommended requirements

- 16 GB of RAM
- A modern quad-core or better processor (roughly, anything sold in the last six or seven years)
- A microphone positioned close to the speaker. A laptop's built-in microphone is usually adequate. If recognition accuracy is poor, try a headset or an external microphone before changing any settings: microphone placement and background noise affect accuracy more than most configuration values do.

### Graphics cards

A graphics card is not required. The default engine runs entirely on the processor and performs well on modern CPUs.

A graphics card helps most on a machine whose processor is older or slower. With an NVIDIA card carrying at least 4 GB of dedicated memory, the Distil-Whisper engine can be installed, which runs speech recognition on the card instead of the processor. The command-line installer offers it only on a machine with such a card; the setup wizard lists it on every machine and quietly installs the default engine instead when the card cannot run it, reporting that on its final page. Only NVIDIA cards support this; on AMD or Intel graphics, use the default processor engine or a cloud engine.

### Estimating performance

- A computer that runs a browser with several tabs and a video call at the same time without struggling is sufficient for Wheelhouse. That is the practical baseline.
- A computer that already responds slowly to basic tasks -- switching windows, typing in a browser -- will show the same delays in Wheelhouse. It still works; responses take longer.
- Installation costs nothing, can be re-run, and can be reversed, so measuring on the machine itself settles the question. If dictated words regularly take 3 to 4 seconds or more to appear, change engines; see [Speech Engines](#speech-engines).

### What speed to expect

- **Modern hardware, default engine:** roughly 1.5 to 2 seconds from speaking to the first word appearing, after which words continue to arrive while you speak rather than at the end of the sentence.
- **NVIDIA graphics card engine:** similar, sometimes slightly faster.
- **Older or slower processors:** 3 to 5 seconds or more to the first word is possible. A cloud engine usually responds faster on such machines, because the recognition work is done elsewhere.

### Improving performance on slower hardware

- Close demanding programs while dictating. Browsers with many tabs, video editors and games compete for the processor the speech engine uses.
- Consider the Google Cloud engine. Almost no recognition work runs on the local machine, so a slower computer still gets fast, accurate results. The trade-offs are the account setup, the privacy difference, and the requirement for an internet connection. See [Speech Engines](#speech-engines).
- Disable unused features. If you have no Sonos speakers and no Sony Bravia TV, leave those plugins disabled -- they are off by default -- so nothing extra runs in the background. See [Plugins](#plugins).
- Slower machines sometimes execute a command before the speaker has finished the phrase. The speech timing values in the settings file can be raised to compensate; see [Settings for slower hardware](#settings-for-slower-hardware) in Configuration.

If none of these produces acceptable response times, the Wheelhouse Assistant can help identify which setting to change for a specific machine; see [Getting Help](#getting-help).

## Voice Commands

Wheelhouse converts speech into keystrokes, text, and system actions. Most commands require no prefix. Commands with destructive or far-reaching effects require the hotword **"x-ray"** first, so that they cannot fire during dictation; those commands are written here with the "x-ray" prefix.

There are two kinds of voice pattern. **Commands** perform an action -- press a key, switch a window, click a button -- and are normally spoken as a complete utterance: say the command, then pause. **Replacements** apply inline during dictation: spoken mid-sentence, the recognized word is replaced with a symbol or corrected text as the text is typed. All punctuation words ("period", "comma", "question mark") are replacements, so dictation does not have to stop to insert punctuation.

### Common Commands

| Say this | What happens |
|---|---|
| undo | Undoes the last action (Ctrl+Z) |
| select all | Selects everything in the current field |
| new line | Inserts a line break without leaving the field |
| backspace | Deletes one character to the left |
| copy | Copies the current selection |
| paste | Pastes whatever is on the clipboard |
| delete word | Deletes the whole word the cursor is on |
| submit | Presses Enter |
| go home | Jumps the cursor to the start of the line |
| go end | Jumps the cursor to the end of the line |

### Usage Examples

**Example 1 -- Dictating and correcting an email**

1. Dictate the body of the message, speaking the punctuation inline: "hi team comma new paragraph the release is ready period"
2. To correct a mistyped word, say **"backspace 2"** to remove the last two characters, then dictate the word again.
3. To correct grammar across a paragraph, select it with **"select paragraph"**, then say **"x-ray fix"**, which sends the selection to the configured AI server and replaces it with the corrected version.
4. Say **"x-ray activate outlook"**, substituting the name of the mail application, to bring its window forward, then **"submit"** to press Enter.

**Example 2 -- Searching for copied text**

1. Select a phrase with the mouse, or say "select word".
2. Say **"copy"**.
3. Say **"x-ray browser"** to bring the default browser forward.
4. Say **"paste"** with the address bar focused, then **"submit"** to press Enter.

### Full Voice Command Reference

Every voice command and replacement is listed, one row each, in the [command and configuration reference](https://wheelhouse-project.org/reference.html). This section covers the behavior a table cannot express: how to dictate a word that is also a command, the key names accepted by "press", and how navigation, punctuation, and clicking behave.

#### Dictation Control

These commands control what is typed and provide a way to dictate a word that collides with a command. "literal [words]" types the words that follow exactly, bypassing all command and replacement processing. "insert [text]" inserts raw text with no capitalization, spacing, or formatting. "submit" presses Enter, and is also recognized as the last word of a sentence: "hello world submit" types "hello world" and then presses Enter. To type the word itself, say "literal submit".

Utterances beginning with "okay Google", "ok Google", or "hey Google" are discarded, so speech directed at a nearby voice assistant is not transcribed.

#### Text Editing

Common mishearings of "undo" and "redo" ("undue", "undu", "redu") are accepted, so the command still fires when the recognizer returns one of those spellings. Deletion counts for "backspace" and "delete" are capped at 50. "tab [number]" requires the number; "tab" alone is typed as the word.

##### The "press [keys]" Command in Detail

"press [keys]" sends any keyboard shortcut. Modifiers are held down first regardless of the order they are spoken, so "press delete control" is equivalent to "press control delete". If any word in the phrase is unrecognized, nothing is pressed and the phrase is discarded rather than typed as text. Hyphenated tokens from the speech engine, such as "f-11" or "control-alt", are split automatically.

**Modifier keys**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: f1 through f12.

**Letters**: any single letter a through z.

**Digits**: a digit is recognized only when another key name follows it. A digit at the end of the phrase is read as a repeat count, so "press control 2" presses Ctrl twice rather than Ctrl+2.

**Symbols by spoken name**: the following are pressed correctly -- backtick, semicolon, slash (forward slash), backslash (back slash), comma, period (dot), single quote (apostrophe), left/right bracket (open/close bracket), equals (equal), minus (hyphen, dash), right parenthesis (close paren). Other symbol names are not reliable in "press": the shifted symbols (colon, tilde, pipe, question mark, double quote, braces, less than, greater than, plus, underscore) produce the wrong character, and left parenthesis, hash, at, ampersand, asterisk, caret, percent, dollar, and exclamation press nothing. To type any of those characters, dictate them as punctuation words instead; see [Punctuation and Symbols](#punctuation-and-symbols) below, which handles every symbol.

**Examples**: "press control shift t", "press f5", "press alt f4", "press windows d", "press left bracket".

#### Text Formatting

Formatting commands apply to the current selection. Select first, with the mouse or with "select word" / "select line", then say the command. The case and shape transforms cover UPPERCASE, lowercase, capitalize, title case, and the programming styles snake_case, camelCase, PascalCase, and kebab-case. The wrapping commands ("parentheses", "brackets", "braces", "angle brackets", "quotes", "single quotes") enclose the selection in those characters; spoken with no selection, they insert an empty pair and place the cursor between the two characters. Words spoken after a wrapping word in the same utterance are wrapped verbatim: symbol words such as "colon" are typed literally rather than converted. Three commands apply character formatting to the selection through the host application's own keyboard shortcuts: "x-ray bold text", "x-ray italics", and "x-ray underline".

#### Navigation

"go" moves the cursor; "grab" moves it while extending the selection. Several moves can be chained in one utterance with "then". The utterance must begin with "go": "grab" is valid only as a step chained after a "go" move, for example "go home then grab to end". Spoken on its own, "grab ..." is typed as dictation.

Counts can be digits ("3") or spoken words ("one" through "ten"; digits are accepted up to 50). "to", "too", and "for" are accepted as sound-alikes for 2 and 4, so "go right to words" moves two words. If any part of a "go" utterance cannot be parsed, the whole phrase is typed as dictation instead, so an unrecognized phrase does not move the cursor.

#### Punctuation and Symbols

Punctuation and symbol words are replacements: they apply inline during dictation. Spoken as part of a sentence, the symbol is typed in place of the word, with no pause required. Every punctuation and symbol word -- period, comma, colon, question mark, and the rest -- behaves this way.

Two mishearing tolerances are built in, because the default local engine frequently mishears "comma" and "colon". Spoken as an entire utterance, **"colin"** inserts ":" and **"come"**, **"kama"**, **"commer"**, or **"come on"** inserts ",". Within a longer sentence these words are dictated normally; the tolerance applies only to a whole utterance. To type one as a standalone word, use "literal come" or "literal colin".

If the recognizer regularly mishears another word, add a personal correction in the Pattern Manager ("x-ray patterns"). It applies inline during dictation like the built-in punctuation words.

#### Application Switching and System

"x-ray activate [app name]" brings the named application's window forward. When a pattern's target is a program file (.exe) and no window is open for it, that program is started; the built-in "x-ray notepad" and "x-ray browser" work this way, and "x-ray browser" resolves to the Windows default browser at the time the command is spoken. The System commands operate on windows and on Windows itself. Four need no hotword: "zoom in", "zoom out", "create tab", and "create window", and "windows settings" opens the Windows Settings application. Four require it: "x-ray close window", "x-ray maximize", "x-ray minimize", and "x-ray desktop", which shows the desktop. In most browsers "create tab" (Ctrl+N) opens a new window rather than a tab, and "create window" (Ctrl+Shift+N) opens a private or incognito window.

#### Mouse Control

**This release has no voice commands that move the mouse pointer.** There is no "mouse up" command and no grid overlay for pointer positioning. Controls are clicked by name or by number instead; see [Voice Element Clicking](#voice-element-clicking) below. Volume and screen brightness are mapped to the thumb wheel of a Logitech MX-series mouse; see [Plugins](#plugins). For pointer-by-voice control, run a separate pointer-control program alongside Wheelhouse, which continues to handle dictation, commands, and clicking by name.

#### Voice Element Clicking

Wheelhouse can click buttons, links, menu items, and other on-screen controls. A control is selected in one of two ways: by its **name**, or by displaying a **number** on every clickable control and saying that number. The numbered overlay covers controls with no spoken name, such as icon-only toolbar buttons, and cases where several controls share a name.

**Clicking by name**: say "click", then the name of the control. "the" may precede the name and is ignored, and a role word may follow it to restrict the search to one kind of control. The "x-ray" hotword prefix is optional on all clicking commands: "click cancel" and "x-ray click cancel" are equivalent. **Role words**: **button**, **link** (a hyperlink), **menu** (a menu item), **tab**, **checkbox** (or **check box**), and **box** / **field** / **input** (a text entry field). With no role word, any clickable control matching the name is considered. A role word spoken with no name, for example "click button", is treated as a name rather than a role, and searches for a control named "button".

**The numbered overlay**: "apply numbers" displays a number on every clickable control in the front window, "click 3" clicks the control labelled 3, and "dismiss numbers" removes the numbers. The numbers remain on screen until "dismiss numbers" is spoken: clicking a numbered control repaints them in place, and they follow whichever window is in front. When "click [name]" matches more than one control closely, the numbers are displayed on those candidates only. While numbers are displayed, a bare spoken number always selects the numbered label, so a control whose own name is a digit -- a calculator "7" -- is reached by adding a role word: "click 7 button" clicks the calculator key, while "click 7" clicks whatever control carries the number 7. If the numbers no longer align after a page scrolls or replaces its content, say "apply numbers" again to repaint them.

**Outcomes**: a successful click produces no notice. A failure produces a brief notice near the floating button and the tray icon: **not found** ("No match for [name]"; nothing matched, and the numbered overlay is the alternative), **ambiguous** (the numbered overlay opens on the candidates; the "Found [A] and [B] -- be more specific" notice appears only when the overlay cannot open), and **could not complete the click** (the wording states the reason: the control is disabled, the click timed out, or the overlay is stale and must be reapplied). Notices are rate-limited, so repeated failures do not produce repeated notices.

#### Wheelhouse Control

These commands control Wheelhouse itself: listening modes, help, personal patterns, and the AI features. "push to talk mode" and "click to talk mode" switch between the two listening modes. "x-ray fix" sends the selected text to the configured AI server for grammar correction and replaces the selection with the result; it requires the AI server to be configured and reachable, announces its progress ("Correcting", "Done"), and leaves the original text in place if the request fails. Five further commands rewrite the selection rather than correcting it: "x-ray simplify", "x-ray shorten", "x-ray make formal", "x-ray pirate", and "x-ray translate to [language]"; see "Rewriting the selected text" under [Selected Commands in Detail](#selected-commands-in-detail), which also covers adding others. "x-ray boost" adds the selected text to the speech recognition hints. "x-ray patterns" opens the Pattern Manager, and "x-ray help" opens the Wheelhouse Assistant in the default browser. "x-ray cancel fix" stops a correction or rewrite that is still running.

Three further commands act on text through the host application: "x-ray find [text]" opens its find box and searches for the words spoken, "x-ray replace" opens its find-and-replace box, and "x-ray search" copies the selection and searches the web for it in the default browser.

Switching the microphone on and off is not a voice command. In toggle mode it is done by clicking either the floating button or the tray icon. In push-to-talk mode listening lasts only as long as the floating button is held; the hold gesture works on the floating button alone, and a click on either surface has no effect. A microphone switch that responded to speech could be switched off by a phrase spoken in passing, leaving no voice route back.

The in-app help chat window is disabled in this release, and the voice patterns that opened it are switched off. "x-ray help" opens the Wheelhouse Assistant in the browser; see [Getting Help](#getting-help).

### Selected Commands in Detail

**"literal [words]"**

Say "literal" followed by the text to be typed, and those words are inserted without being processed against any command or replacement pattern. This is how to dictate a phrase that would otherwise trigger a command: "literal copy" types the word "copy" instead of copying, "literal period" types the word instead of a full stop, and "literal new line" types the phrase instead of inserting a line break.

"literal" takes effect wherever it appears in an utterance: everything after it is typed exactly as spoken, and "literal" itself is not typed. To type the word "literal", say "literal literal".

**"x-ray boost"**

When the speech recognizer repeatedly mishears a specific word -- typically a name, a product, a place, or a technical term -- select that word anywhere on screen, with the mouse or with "select word", and say **"x-ray boost"**. The selection is copied and saved as a recognition hint in a shared hints file, which persists across restarts, so each word needs to be boosted once. Hints are capped at 100 characters; boost individual words or short phrases rather than sentences.

**Saving a hint and applying it are separate.** **Parakeet, the default engine, saves the hint but does not apply it.** Hint biasing is disabled by default because applying hints slowed recognition by roughly 25 percent per utterance in the project's measurements. To make Parakeet apply saved hints, set enabled = true under [hotwords] in the Parakeet engine's own config file and restart Wheelhouse, accepting the slower recognition. Until that setting is changed, boosting does not affect what Parakeet recognizes. **Google Cloud Speech-to-Text** applies saved hints without further configuration. **Distil-Whisper** saves hints but never applies them, because hint biasing degrades that engine's recognition.

**"x-ray patterns" (the Pattern Manager)**

This opens the **Pattern Manager**, which lists every voice command and text replacement, grouped by category. Selecting an entry shows its trigger phrase, the action it performs, and whether it requires the hotword.

The Pattern Manager can **view** any pattern, including every built-in one; **create** personal patterns, such as a shortcut that types an email address, a correction for a word the engine mishears, or a command that opens a program; **edit** and **delete** user-created patterns; **customize** a built-in pattern, where a personal copy with the same trigger overrides it and deleting that copy restores the shipped behavior; and **change the command hotword** from "x-ray" to another word.

Personal patterns are stored in a separate per-machine file, so they are preserved across upgrades, and the shipped patterns file is not modified.

**Rewriting the selected text**

With text selected, **"x-ray simplify"** sends it to the AI server to be rewritten in plain language and replaces the selection with the result. **"x-ray shorten"** removes repetition and padding, **"x-ray make formal"** removes contractions and casual wording, and **"x-ray pirate"** rewrites the text in pirate speech. **"x-ray translate to [language]"** translates the selection into the language named, for example "x-ray translate to spanish" or "x-ray translate to brazilian portuguese"; names of more than one word are recognised. All five use the same AI server as "x-ray fix" and leave the original text in place if the request fails. They announce themselves as "Rewriting" where "x-ray fix" announces "Correcting"; both then say "Done".

A rewrite pastes back plain text, so character formatting applied in a word processor -- bold, italics, a font, a colour -- is lost on the rewritten part. Layout is preserved: line breaks, blank lines, indentation, bullets, numbering, and lines that are not sentences, such as a code line or a postal address, are returned unchanged.

**Adding a rewrite command.** The five commands above are one action carrying a different instruction each, so further rewrite commands can be added from the Pattern Manager without changing program code. Create a pattern, choose the **Rewrite text with AI** action, and set its single parameter to a sentence addressed to the AI describing the style required.

For example, to rewrite text at a reading level a younger reader can follow, create a pattern with the trigger **reading level** and this instruction:

> Rewrite this text so a ten-year-old could read it. Use short sentences and everyday words, and explain any term a ten-year-old would not know. Keep every fact. Return only the rewritten text.

Selecting a paragraph and saying "x-ray reading level" then rewrites it. Two constraints apply to that instruction. **Describe the style only**: Wheelhouse supplies the wording that preserves layout, and the wording that prevents instructions inside the selected text from redirecting the AI. Neither needs to be written into the instruction, and duplicating them degrades the result. **End with "Return only the rewritten text."** Without it, the AI is liable to prefix its answer with a sentence of commentary, which is then pasted into the document.

**"x-ray cancel fix"** stops a request that is still running -- a correction or any of the rewrites -- and nothing is pasted.

**"x-ray help"**

Opens the Wheelhouse Assistant, the project's online help, in the default browser, where questions can be asked in plain language. The address is the gem_url setting in the [ai.help] section, which points at the assistant by default.

## Speech Modes

Wheelhouse has no command mode and no dictation mode to switch between. Each utterance is classified as it arrives, using the position of the words within the phrase.

### Classification of spoken input

- **Command**: the utterance matches a voice command and the command is performed. "undo" presses the undo shortcut; "delete five" deletes five characters. Nothing is typed.
- **Dictation**: the utterance is typed into the focused text field. "dear Sarah thank you for the update" is typed as those words.
- **Inline replacement**: certain words are replaced with symbols or corrected spellings within dictation. "hello comma world" produces "hello, world"; the word "comma" is replaced by the punctuation mark rather than typed.

### Word position determines classification

- **The first word of a phrase is a candidate command.** When speech starts after a pause, the first word is checked against the set of known command openings. If it could begin a command, it is held briefly -- well under a second -- to see whether the following word or two completes one. "delete five" spoken as its own phrase runs the command. If the words match no command, they are typed as ordinary text; no words are discarded.
- **Words in the middle of a phrase are dictation.** "I want to delete five items" is typed in full, including the word "delete", because "delete" did not begin the phrase.
- **Replacement words apply in any position.** Words such as "comma" and "period" are replaced whether they occur first, last, or mid-sentence, since their purpose is to appear within dictation.

### Hotword-protected commands

Commands with destructive effects, such as closing a window, run only when the utterance begins with "x-ray", as in "x-ray close window". Low-risk commands do not require it. The hotword follows the same position rule: "x-ray" carries its special meaning only as the first word of a phrase, and is typed as text anywhere else. If "x-ray" is followed by something that is not a command, the whole phrase including "x-ray" is typed as text.

### Streaming insertion

Recognized words are inserted while speech continues, rather than after the utterance ends. The exceptions are the brief hold at the start of a phrase while a command match is evaluated, and a similar hold around replacement words. Both are fractions of a second.

### Chaining cursor moves with "then"

Cursor movements and text selections can be chained into one phrase with "then":

- "go home then grab to end" -- moves to the start of the line, then selects to the end of the line.
- "go top then grab to bottom" -- moves to the top of the document, then selects to the bottom.

Chaining applies only to the "go" (move the cursor) and "grab" (extend the selection) navigation commands. All other commands, including copy, paste, and window switching, are spoken as separate phrases.

## Interaction Modes

Speech modes, described above, govern how recognized words are classified. Interaction modes govern when Wheelhouse listens. There are two, and they can be switched at any time.

### Toggle mode

Wheelhouse listens continuously while speech is switched on. One click on the floating button, or one left-click on the tray icon, switches listening off; another click switches it back on. This is the mode for hands-free operation: once listening is on, no further input device is required.

In toggle mode, pressing and holding the floating button for about a fifth of a second or longer restricts listening to the duration of the hold. This is useful when listening is normally off and a single command is to be spoken.

### Push-to-talk mode

Wheelhouse listens only while the floating button is held down. Press and hold to speak; releasing stops listening. During the hold, the computer's speakers are muted, so that sound from a video or from music cannot reach the microphone and be transcribed; the previous volume is restored on release. In this mode a single left-click on the tray icon has no effect; the hold operates on the floating button.

Three constraints apply:

- **Audio already playing takes precedence.** Listening is suspended while the computer is playing sound, and starting a hold does not override that. A hold begun while a video or music is playing receives no audio, and the button shows that listening is off. Pause the audio first; listening resumes shortly after it stops. The speaker mute described above keeps subsequent audio out of a hold begun in silence, and does not enable a hold begun while audio is playing.
- **Safety release.** If a release is never registered, listening stops after 30 seconds and the previous audio state is restored, so the microphone is not left open and the speakers are not left muted. If that cutoff interrupts long dictations, raise ptt_safety_timeout_seconds in the [speech] section of the settings file.
- Push-to-talk requires a hand on the mouse or a finger on a touchscreen, so it is not hands-free.

### How to switch between the modes

Any of the following, at any time:

- **By voice**: "push to talk mode" switches to push-to-talk; "click to talk mode" switches back to toggle mode.
- **From the menu**: right-click the floating button or the tray icon -- both open the same menu -- and click "Push-to-Talk Mode". A checkmark on that item indicates that push-to-talk is active.
- **Double-click**: double-clicking the floating button or the tray icon alternates between the two modes.
- **At startup**: the interaction_mode setting in the [speech] section of the settings file, either "toggle" or "push_to_talk", selects the mode Wheelhouse starts in. The voice, menu, and double-click switches change it while Wheelhouse is running.

### Choosing a mode

Toggle mode is the default and supports hands-free operation. Push-to-talk suits a noisy room, an environment where other people's speech or the computer's own audio is being transcribed, and occasional voice input where nothing should be recognized between holds.

## Floating Button and Tray Icon

Wheelhouse presents two on-screen controls: a small round button that floats above other windows, and the Wheelhouse icon in the system tray near the clock. Both switch listening on and off, both switch between the two interaction modes, and both open the same right-click menu. The floating button additionally reports the current state, and it is the one that can be moved and resized.

### The floating button

The button is a coloured circle that remains above other windows. Its colour reports the current state:

- **Dark grey**: Wheelhouse is starting and has not yet determined whether the speech engine is ready.
- **Light grey**: listening is off.
- **Blue**: push-to-talk mode, button not held. Press and hold it to speak.
- **Solid red**: listening is on.
- **Pulsing red to orange**: speech is being received.
- **A brief flash of green**: the last utterance has been processed.
- **A small white dot in the lower-right corner**: a requested control is being searched for in the window. The dot is drawn over whichever colour is currently showing.

Available actions:

- **Click** to switch listening off, and click again to switch it on. In push-to-talk mode a click has no effect; that mode listens only during a hold.
- **Press and hold** for about a fifth of a second or longer to listen for the duration of the hold, stopping on release. This works in either interaction mode.
- **Double-click** to switch between toggle mode and push-to-talk mode.
- **Drag from the middle** to move the button. Its position is saved and restored at the next start.
- **Drag the outer edge** to resize it. It resizes around its own centre, so the point being dragged keeps its position relative to the middle. Both the size and the resulting position are saved.
- **Hold Ctrl and roll the mouse wheel** over it to resize it. Both gestures use the same limits.
- **Right-click** to open the menu described below.

The resize area is a ring 8 pixels wide around the outer edge. The area inside that ring moves the button and starts push-to-talk. The pointer changes to a diagonal arrow over the ring. On a small button the ring narrows to a third of the radius, so that a movable centre always remains. The button is constrained to between 15 and 150 pixels across. If a resize would place the button off the edge of the screen, it is moved back into view.

To hide the button, right-click it and switch off "Show Floating Button". The same menu item on the tray icon restores it.

### The tray icon

The tray icon is the Wheelhouse logo in the notification area near the clock. Its appearance is fixed and does not change with the state of the program. The floating button reports state; the tray icon keeps Wheelhouse reachable when the floating button is hidden.

- **Left-click** to switch listening off and on, as with the floating button. In push-to-talk mode a left-click has no effect; the hold gesture works only on the floating button.
- **Double-click** to switch between toggle mode and push-to-talk mode.
- **Right-click** to open the menu below.

If the icon is absent, Wheelhouse did not finish starting; see [Troubleshooting](#troubleshooting).

### The right-click menu

The floating button and the tray icon open the same menu. Most items are unavailable until Wheelhouse has finished starting. "About Wheelhouse" is always available, because it depends on no other part of the program.

- **Speech Enabled** -- switch listening on or off. The checkmark shows the current state.
- **Show Floating Button** -- show or hide the floating button. The checkmark shows whether it is visible.
- **Interim Results** -- selects whether words are typed as they are recognized and corrected afterwards, or held until the phrase ends. The first is the streaming insertion described under [Speech Modes](#speech-modes); switching this off trades the immediate feedback for text that arrives already settled.
- **Push-to-Talk Mode** -- switch between the two interaction modes. The checkmark shows when push-to-talk is active.
- **STT Provider** -- select the speech engine. Only engines set up on this computer are listed. See [Speech Engines](#speech-engines).
- **Google Cloud Credentials** -- choose the Google service-account key file. Shown only when the Google Cloud engine is set up on this computer. See [Speech Engines](#speech-engines).
- **AI Model** -- select the AI model, when AI features are configured. If the model named in the settings file is no longer offered by the server, the menu reports that rather than substituting another model.
- **Pattern Manager** -- open the editor for personal voice patterns. See [Voice Commands](#voice-commands).
- **Debug** -- switch detailed logging on or off. Leave it off except when diagnosing or reporting a problem.
- **Help** -- open the Wheelhouse Assistant in the browser. This is the same page the spoken command "x-ray help" opens.
- **About Wheelhouse** -- show the program name and the running version. Include the version in any problem report.
- **Restart Transcription Service** -- restart the speech engine without restarting the rest of Wheelhouse. This is the first step when speech recognition stops responding.
- **Restart Wheelhouse** -- restart the whole program.
- **Exit** -- close Wheelhouse. Required before running the installer to update, and before uninstalling.

## Configuration

No settings need to be edited to use Wheelhouse. Every value ships with a working default, and the most common choices -- which speech engine to use, push-to-talk versus click-to-talk -- can be changed from the floating button's or the tray icon's right-click menu without opening a file. The listening mode can also be changed by voice; the speech engine cannot. This section covers the adjustments available in the settings file.

**Where the settings file is.** Wheelhouse keeps its settings in a plain text file named config.toml, which opens in Notepad. It is at:

```
%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\config.toml
```

To open the folder, press the Windows key and R together, paste `%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse` into the box, and press Enter. `%LOCALAPPDATA%` expands to `C:\Users\<your user name>\AppData\Local`, a folder Windows hides by default, which is why pasting the path is easier than browsing to it. The installer creates config.toml from the template `config.toml.example` in the same folder. Your copy is personal to your machine and is never transmitted. Lines starting with a number sign are comments, and the file documents many of its own settings inline.

A few practical notes:

- Change one setting at a time, then restart Wheelhouse so the change takes effect.
- To restore the defaults, copy `config.toml.example` over `config.toml` in that same folder.
- Settings marked "device-specific" are off by default and apply only if you own that hardware.

**The complete per-setting reference** -- every configuration key, its default, and what it does -- is in the [command and configuration reference](https://wheelhouse-project.org/reference.html). Two settings are worth knowing before opening it. Transcript logging (LOG_TRANSCRIPTS) is off by default, which keeps dictated words and clipboard contents out of the log files; turn it on only while diagnosing a recognition problem, then turn it back off. The AI server's API key is never stored in config.toml: if your server requires one, set the WHEELHOUSE_AI_API_KEY environment variable instead, so the key is not held in a settings file that could be copied or shared.

The rest of this section covers the two most common adjustments: performance on slower hardware, and recognition quality. For a setting not covered here, the Wheelhouse Assistant answers questions about any key in the reference; see [Getting Help](#getting-help).

### Settings for slower hardware

If Wheelhouse feels laggy or unreliable on an older computer, these changes help, roughly in order of impact:

1. **Use the default speech engine.** "parakeet_tdt" ([stt] last_provider) is the lightest local engine and runs on any CPU; do not switch to "distil_medium_en" without a capable recent graphics card. If even the default struggles, "google_stt" moves the work to the cloud -- at the cost of an account and an internet connection.
2. **Give yourself more speaking time.** Raise REPLACEMENT_TIMEOUT_MS and COMMAND_TIMEOUT_MS from 700 to 900-1000, and COMMAND_COMPLETION_WAIT_MS from 1000 to 1500 if quick back-to-back commands collide.
3. **Slow down text insertion.** Under [ui_actions.timing], raise post_paste_delay_ms (30 to 60), clipboard_operation_delay_ms (50 to 100), and clipboard_verification_timeout_ms (250 to 500) if dictated text arrives incomplete or garbled.
4. **Give voice clicking more time.** Under [click], raise response_timeout_ms (3000 to 5000) and walk_deadline_ms (2500 to 4000) if clicks time out in complex windows.
5. **Allow a local AI server longer to answer.** Raise [ai.server] timeout_s from 30 to 60 if corrections time out -- or leave AI off; nothing else depends on it.

### Speech recognition quality settings

**The hallucination filter (Distil-Whisper engine only).** Whisper-family speech engines have a well-known quirk: fed a cough, a throat-clear, or background noise, they sometimes invent polite filler -- a stray "thank you" or "okay" you never said. The Distil-Whisper engine ships with a confidence filter that discards such low-confidence utterances instead of typing them. Its threshold is **hallucination_logprob_threshold** (default -0.55) in the Distil-Whisper provider's own config file, not the main config.toml. That default was calibrated on a single male voice with a studio microphone, so it may be too strict for other voices: if real speech is sometimes silently ignored -- more likely with a strong accent, quiet speech, or a laptop microphone -- lower it to -0.7 or -0.8. More negative means more permissive: fewer real words discarded, the occasional phantom "thank you" let through; a very large negative number turns the filter off entirely. If no threshold produces acceptable results, switch to the Google engine from the menu on the floating button or the tray icon; it is less affected by background noise and by variation between voices. It is a cloud service: it requires an account and sends audio to Google. The filter does not apply to the default Parakeet engine, which neither produces the confidence signal it relies on nor shares the quirk to the same degree.

## Plugins

Plugins are optional add-ons that connect Wheelhouse to extra hardware and services: your laptop screen, Sonos speakers, a Sony TV, and a few Windows features. Every plugin has its own `[plugins.*]` section in config.toml with an `enabled` switch, so you can turn each one on or off without deleting anything. You do not need any of them for dictation and voice commands to work. A plugin that cannot find its hardware at startup turns itself off for the session and reports the reason in the log; the rest of Wheelhouse runs unaffected. Hardware that is present but unreachable is a different case: the Sonos plugin keeps polling a speaker whose address it resolved at startup, so a speaker that comes back on the network is picked up again without a restart.

Four of these plugins respond to the mouse thumb wheel -- the small horizontal wheel on the side of the mouse, under your thumb. This is not the main scroll wheel: that one keeps its normal scrolling job. Wheelhouse reads the thumb wheel directly from the device, which currently works only with Logitech MX-series mice. Screen zones pick what the thumb wheel controls: pointer at the left edge of the screen, it adjusts brightness; anywhere else, volume -- no command or click needed. Step size and zone width are adjustable in the configuration reference.

### Internal Panel

Controls the brightness of a laptop's built-in screen from the brightness scroll zone. Enable or disable with `plugins.internal_panel.enabled` (default: enabled). There are no other settings -- everything is detected automatically. It talks to the laptop display through a built-in Windows interface, entirely on your own machine. On a desktop PC with no built-in panel it finds no display to control and stays inactive for the session.

### Sonos

Adjusts Sonos speaker volume from the volume scroll zone, and pauses Wheelhouse's listening while a music service is playing on the Sonos, so song lyrics are not transcribed into your documents. Enable with `plugins.sonos.enabled` (default: disabled).

Volume control through this plugin requires a specific arrangement: a Sonos sound bar connected to the display this computer uses, receiving the computer's or the display's audio. Wheelhouse sends volume commands to the Sonos only when two conditions hold at once -- Windows is playing to an external audio device rather than the machine's own speakers, and the Sonos reports that it is receiving television audio. In any other arrangement, including Sonos speakers elsewhere in the house, volume commands go to the normal Windows volume instead. Settings:

- `polling_interval` -- how often, in seconds, to check whether music is playing (default 2).
- `speaker_ip` -- optional. Automatic discovery runs once at startup, and only when the Windows output device is an external one: Wheelhouse reads the name of the default output device and skips discovery when that name looks like built-in hardware (Realtek, Intel, "Speakers", "Headphones", and similar). A discovered speaker wins over this setting; set it when discovery finds nothing.
- `request_connect_timeout` / `request_read_timeout` -- advanced network timeouts (defaults 2.0 and 5.0 seconds); rarely need changing.

It connects to the speaker over your home network directly -- no Sonos account or internet service is involved.

The pause this plugin applies is narrow: it fires only when the Sonos is playing from a music service, and not when the Sonos is playing audio it received from this computer or from the television. That is deliberate, so that watching a film does not stop Wheelhouse from listening.

A second, separate mechanism does pause listening for computer audio, and it is on by default whether or not you own a Sonos. Wheelhouse watches the sound level of the Windows output device and pauses recognition while sound is playing through it (`ENABLE_AUDIO_SUPPRESSION`, default true). The check runs on an interval that widens to ten seconds while audio is playing, so listening can take up to that long to resume after the audio stops. So audio your computer plays does pause listening -- including audio it plays through a Sonos, when the Sonos is the Windows output device. What the Windows output device never sees, and therefore never pauses listening for, is audio that reaches the Sonos without passing through this computer: television audio over HDMI or an optical cable, and music the Sonos streams by itself.

### System Volume

Controls the normal Windows volume (the same one as the taskbar speaker icon) from the volume scroll zone, and quiets system audio while you hold the push-to-talk button. Enable with `plugins.system_volume.enabled` (default: enabled). Settings:

- `device_type` -- which audio device to control: `"default"` (the usual choice), `"communications"`, or a specific device name.
- `volume_step_db` -- loudness change per wheel step, in decibels (default 1.5).
- `min_volume_db` / `max_volume_db` -- the volume floor and ceiling (defaults -96.0 and 0.0).

Fully local, no network. Both volume plugins can stay enabled: at startup Wheelhouse picks one to receive volume commands -- Sonos when the Windows output device is external and a discovered Sonos reports that it is receiving television audio, System Volume in every other case.

### Bravia (Sony TV)

Brings a Sony Bravia TV used as a computer monitor into Wheelhouse's brightness control, so the brightness scroll zone can dim and brighten the TV itself. Enable with `plugins.bravia.enabled` (default: disabled). Settings:

- `ip_address` -- your TV's address on the home network. Optional: leave it blank and Wheelhouse searches the network for the TV automatically; set it if you have more than one TV or discovery fails.
- `psk` -- the pre-shared key you set on the TV under Settings -> Network -> Home Network -> IP Control. Required; the plugin will not start with it blank.
- `device_name` -- the TV's audio device name exactly as Windows shows it under Sound settings -> Output (default "SONY TV"). This is not a label you invent: Wheelhouse uses it to look the device up for spatial-sound handling, so it must match the Windows name exactly.

It connects to the TV over your home network using Sony's built-in remote-control interface. The plugin first checks whether a Sony display is physically connected; on a machine without one it stays inactive for the session and issues no network requests.

### Idle Monitor

Notices when you have stepped away (no keyboard or mouse activity) and pauses listening so Wheelhouse is not transcribing an empty room; listening resumes when you return or say the wake word. Enable with `plugins.idle_monitor.enabled` (default: enabled). Settings: `idle_timeout_minutes` (default 10) and `polling_interval_seconds` (default 4). Fully local -- it only asks Windows how long since your last keypress or mouse move. Note that the measure is keyboard and mouse activity, not sound: watching a film without touching either pauses listening once the timeout passes.

### Window Positioning

Automatically moves the Windows On-Screen Keyboard out of the way when it would cover the window you are working in. Enable with `plugins.window_positioning.enabled` (default: enabled). Settings: `target_window_names` (which windows to move; default is the On-Screen Keyboard), `move_cooldown_seconds` (default 0.5, prevents jitter), `clearance_gap_pixels` (default 5), and `ignore_window_titles` / `ignore_window_classes` (windows that should never trigger a move). Fully local.

**Administrator rights.** Windows does not allow a program to move the window of a program running at a higher privilege level. If the on-screen keyboard runs as administrator and Wheelhouse does not, the keyboard does not move, nothing is reported on screen, and the reason is recorded only in the debug log. To make the move work in that case, run Wheelhouse as administrator as well. This applies only when the keyboard itself was started with administrator rights, which is not how Windows normally starts it.

### Example configuration

```toml
[plugins.system_volume]
enabled = true

[plugins.internal_panel]
enabled = true

[plugins.idle_monitor]
enabled = true
idle_timeout_minutes = 10

[plugins.sonos]
enabled = false        # set true only if you own Sonos speakers

[plugins.bravia]
enabled = false        # set true only if a Sony Bravia TV is your monitor
ip_address = ""        # optional; found automatically when blank
psk = ""               # the pre-shared key from the TV's IP Control settings
device_name = "SONY TV"  # must exactly match the device name in Windows
                         # Sound settings -> Output; it is a device lookup key
                         # for spatial-sound handling, not a free-form label
```

### Plugin troubleshooting

- Confirm the plugin's `enabled = true` and restart Wheelhouse -- plugins are only discovered at startup.
- Check the log's startup lines: each plugin reports whether it initialized, went inactive (hardware not found), or failed, usually with the reason.
- For Sonos and Bravia, make sure the device is powered on and reachable from this PC on the same network.
- For Bravia specifically, IP Control must be enabled on the TV and the pre-shared key in config.toml must match the one set on the TV.
- If the mouse wheel does nothing, check the scroll zones: pointer on the left side of the screen adjusts brightness, anywhere else adjusts volume -- and at least one plugin for that control type must be enabled.

## Troubleshooting

Run the verification checks below first, then read the entry matching the check that failed. The Wheelhouse Assistant can also read an error message or a log excerpt and identify the cause; see [Getting Help](#getting-help).

### Setup verification

Run these five checks in order. Stop at the first that fails and read the entry it names.

1. **Did the installer finish without error lines?** If not, see "Installer failures" below.
2. **Do Windows Sound settings show the microphone receiving sound?** Right-click the speaker icon on the taskbar, open Sound settings, open Input, and speak. If the input meter does not move, see "Microphone not detected."
3. **Is the Wheelhouse icon present in the system tray?** The tray icon is the check that matters here. The floating button can be switched off from the menu on either the button or the tray icon, so a missing floating button does not mean the program failed to start. If the tray icon is missing, see "Wheelhouse does not start and neither the tray icon nor the floating button appears."
4. **Open Notepad, click in the empty page, and say "hello".** If the word does not appear, see "Dictation not appearing in text fields."
5. **Say "undo".** If the word does not disappear, see "Commands not recognized."

If all five pass, the installation is working, and any remaining problem is specific to one application or one feature. The entries below cover the common cases.

### Common Problems

**Microphone not detected**

- *Symptom:* Wheelhouse starts, nothing happens when you speak, and Windows Sound settings show no input activity.
- *Likely cause:* Windows is using a different microphone, or a privacy setting is blocking desktop applications from the microphone.
- *Action:* Open Settings > Privacy and security > Microphone and confirm that "Let desktop apps access your microphone" is on. Then open Sound settings > Input and select the microphone in use. Restart Wheelhouse afterward.

**Wheelhouse does not start and neither the tray icon nor the floating button appears**

- *Symptom:* Wheelhouse is started and nothing appears. The tray icon is the one to judge by, because the floating button is hidden whenever "Show Floating Button" is switched off.
- *Likely cause:* One of the background processes failed during startup, most often because a speech model is missing or an earlier install was interrupted.
- *Action:* Re-run the installer. It repairs a broken install and preserves settings and personal data. If it still does not start, restart the computer and try once more before filing a report.

**Speech engine not connecting**

- *Symptom:* The speech engine is reported as disconnected, or Wheelhouse remains in a waiting state without recognizing speech.
- *Likely cause:* The speech engine failed to start. Common reasons: its model was never downloaded, the Google Cloud engine has no credentials, or the computer is low on memory.
- *Action:* Switch engines from the menu on the floating button or the tray icon. Parakeet is the built-in offline engine and requires no account. If the required engine was never set up, re-run the installer and select it at the engine question. For the Google Cloud engine, check the credentials: open the menu on the floating button or the tray icon, choose **Google Cloud Credentials**, and select the key file. That setting takes precedence, and the older GOOGLE_APPLICATION_CREDENTIALS environment variable is consulted only when no file has been chosen from the menu. See [Speech Engines](#speech-engines). If the engine will not start immediately after an install or update, open a new PowerShell window and run "uv --version". If uv is not found, the installer's tooling is not on the PATH; re-running the installer corrects that.

**Commands not recognized**

- *Symptom:* A command such as "maximize" has no effect, or is typed as text.
- *Likely cause:* The speech engine returned a different word, for example "maximum" instead of "maximize", or the utterance overlapped with playing audio.
- *Action:* Speak the command as a separate utterance, with a brief pause before it. Speak at normal conversational volume; raised volume reduces recognition accuracy. If one word is misheard repeatedly, select a correctly spelled copy of it and say "x-ray boost". See its entry in [Voice Commands](#voice-commands); on the default engine the hint is saved but is applied only after hint biasing is enabled.

**Command words are typed as text instead of running**

- *Symptom:* "close window" is typed into the document instead of closing the window.
- *Likely cause:* Expected behavior. Destructive commands require the hotword "x-ray" first, so that they cannot fire during dictation.
- *Action:* Say "x-ray close window". The command reference marks which commands require the hotword.

**Dictation not appearing in text fields**

- *Symptom:* Speech is recognized but no text appears in the application. A notice may appear stating that the focused location could not be confirmed to accept text.
- *Likely cause:* Either the text field is not focused, or the focused control could not be confirmed to be a text field. Typing into a non-text control in some applications, particularly web browsers, triggers keyboard shortcuts instead of entering text, so the insertion is refused rather than attempted.
- *Action:* Click inside the text field and try again. If a notice appears with a "Try it anyway" button, use it; after the text lands correctly several times, that control is added to the approved list and the notice stops appearing. If dictation works in Notepad, the problem is specific to that application's text field.

**Real speech ignored, or short dictations dropped**

- *Symptom:* With the Distil-Whisper (graphics card) engine, short phrases -- or for some voices a substantial part of normal speech -- produce no text and no error.
- *Likely cause:* That engine's confidence filter, calibrated on one voice with a studio microphone, classifies real speech as noise and discards it. This is more likely with a strong accent, quiet speech, or a laptop's built-in microphone.
- *Action:* Lower the threshold: in the Distil-Whisper engine's own config file, change hallucination_logprob_threshold from -0.55 to -0.7 or -0.8, where more negative is more permissive, then restart Wheelhouse. If no threshold produces acceptable results, switch engines from the menu on the floating button or the tray icon; the Google Cloud engine does not use this filter.

**AI text correction does nothing or times out**

- *Symptom:* Selected text is not corrected although AI is enabled. Text correction and rewriting are the AI features in this release; the in-app help chat is currently disabled.
- *Likely cause:* Wheelhouse sends requests to an AI server rather than running the model inside itself. That server is either one you point it at, or one it starts on this machine when [ai.runtime] enabled is true and the command-line installer was run with `-AiMode local`. If the server is absent, unreachable, slow, or does not provide the requested model, the AI features disable themselves and the rest of the program continues to run.
- *Action, in order:* confirm that [ai] enabled = true and that [ai.server] base_url is set, since an empty base_url disables AI by design; confirm the server is reachable at that address and that the [ai.server] model name is one it provides; raise [ai.server] timeout_s if the server is slow to respond; and for a remote server requiring a key, set the WHEELHOUSE_AI_API_KEY environment variable -- the key is never stored in the settings file -- and restart Wheelhouse.
- *Note:* An unreachable AI server does not affect dictation, voice commands, or any other feature.

<!-- install-doc:start -->

### Installer troubleshooting

**Installer failures**

Each installer failure message, and the action for it, is listed under [Installation failure messages](#installation-failure-messages). Re-running the installer is safe, interrupted downloads resume, and the messages contain no personal data and can be included in a help request.

<!-- install-doc:end -->

---

## Getting Help

**The Wheelhouse Assistant is the first place to ask.** It is a ChatGPT assistant holding this entire document, the full command and configuration reference, and the project's own notes on how each part behaves. It answers questions in plain language, reads an error message or a log excerpt and identifies the likely cause, and names the specific setting to change and the file it belongs in. It covers material this document summarizes, so it can answer questions no section here addresses.

<https://chatgpt.com/g/g-6a5ab92068d0819198db2a83135b9540-wheelhouse>

Three ways to reach it: the address above, the **Help** item in the right-click menu on the floating button or the tray icon, and the spoken command "x-ray help". All three open the same assistant in the default browser. It requires a ChatGPT account; the free tier is sufficient.

The assistant does not have access to a particular computer, so it cannot read logs that are not pasted into it, and it does not know about changes made after the release it was built from.

**Reporting a defect.** A problem in Wheelhouse itself -- something that behaves incorrectly rather than something that needs explaining -- goes to the project:

- Open an issue or start a discussion at https://github.com/wheelhouse-project/Wheelhouse.
- Or email help@wheelhouse-project.org.

Include the Wheelhouse version from **About Wheelhouse** in the right-click menu, what was done, what was expected, and what happened instead. Paste any error message in full, and attach the installer's setup log if the installer failed. Installer messages and log lines contain no dictated text unless transcript logging was switched on; see [Configuration](#configuration).

---

Generated: 2026-07-30 for the v1.0.6 release
Wheelhouse version: 1.0.6
