# Wheelhouse Installation Guide

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
  provided to you -- this help document, the installation guide, and the separate Wheelhouse
  action, notice, and configuration reference when they are provided. Never invent features,
  commands, or settings that none of the provided documents describe. For the
  exact wording of a voice command, use the tables in the Voice Commands section
  of this help document, which list every command. For a configuration setting
  and its default value, use the configuration reference when it is available:
  this help document explains the settings in prose, but the complete list of
  every setting lives in that reference.
- For installation, updates, removal, or installer troubleshooting, use the
  separate installation guide (wheelhouse_install.md).
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
  version" line; it is an internal build identifier. The separate action, notice, and
  configuration reference names its own release in its own "Generated" footer
  line the same way.
- When describing voice commands, always give an example of what to say.
- When a user seems overwhelmed, direct them to the "Quick Start" section
  and tell them to ignore everything else until they're comfortable.
- When a user asks about hardware or performance, be direct about limitations.
  Don't promise it will work on every machine.
- Greet the user and ask what they need help with.

---

## Installation and Setup

This section describes how to install Wheelhouse, update it, and remove it. It also lists the checks to perform after the first start.

<!-- install-doc:start -->

### Running the installer

The installer is a standard Windows setup wizard. Download the installer and run it. You do not need to install anything ahead of time. The download address is:

https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/Wheelhouse-Setup.exe

If Windows shows a "Windows protected your PC" screen, see [Security warnings](#security-warnings) below. The whole process takes about 10 to 20 minutes. Most of that time is for the download, which is roughly 3 GB in total. The wizard does these steps:

1. Asks its questions first. It asks which speech engine to use. The pre-selected answer suits most installations (see [Speech Engines](#speech-engines)). It asks whether to set up the optional AI helper. The wizard offers one AI choice, a cloud model from Google, and the choice to skip it. To set up the model that runs on your own machine, use the command line instead, as described below. It asks whether Wheelhouse starts when you log in and right after setup finishes, and both answers are pre-selected. It also asks you to turn on microphone access for desktop apps, but only when that Windows setting is off. When the setting is on, setup says nothing about it.
2. Checks the requirements in [What you need](#what-you-need). Four of the requirements stop setup when the computer does not meet them. These are 64-bit Windows, the Windows version, free disk space, and enough memory for the chosen speech engine. In each case setup shows on the screen what is missing and what to do about it. The other requirements are the processor core count, a connected microphone, and the recommended 16 GB of memory. When the computer does not meet one of these, setup shows a notice and continues.
3. Installs the Microsoft Visual C++ runtime that the speech engine needs, but only when the computer does not have it or has a copy that is too old. Windows asks for permission first. Then the progress window of the Microsoft installer appears. If you decline, or if the runtime install fails, setup continues. The final page of setup says what happened and what to do.
4. Installs uv, the environment manager that Wheelhouse uses, into the user profile.
5. Downloads the Wheelhouse application, verifies the download against its published fingerprint, and creates the Python environments of Wheelhouse. Those environments are self-contained and separate from any other Python installation on the computer.
6. Downloads the offline speech model if you keep the default engine. The download is 2.5 GB. This is the longest step.
7. Creates Start-menu and desktop shortcuts.
8. Reports on its final page any information that is useful to you. Setup can complete and still change something. For example, setup can install Parakeet in place of a speech engine that Wheelhouse no longer includes. These notices appear on the final page, and not only in the setup log.

Wheelhouse installs for one user account. You do not need administrator rights. The one change outside your user account is the Microsoft Visual C++ runtime in step 3, and Windows installs it for the whole computer after the permission prompt. If you cannot approve that prompt, setup still finishes. The final page says that the runtime was not installed.

**Running the wizard again to update.** When Wheelhouse is already installed, the AI helper page shows "Leave my AI helper setting unchanged." in place of "Skip for now (recommended). You can set this up later." The wizard pre-selects that choice. The choice keeps the current AI helper setting and any saved access key as they are. This includes a local AI model that you set up from the command line. The other choice, the Google cloud model, replaces the current setting.

**Command-line installation.** The same install runs as one PowerShell line. The line asks only the speech-engine question, the start-at-login question, and the start-now question as text prompts. Both start questions default to no. On a computer with less than 8 GB of memory, a question that offers the Google Cloud engine replaces the speech-engine question. An update of an install that already uses the Google Cloud engine asks neither question on such a computer, and it keeps that engine.

The line asks nothing about the AI helper. You give the AI choice as an argument instead, or you leave it out. With no argument, a first install has no AI, and an update keeps the current AI setting. The one-line command cannot carry arguments. To pass an argument, download install-wheelhouse.ps1 from the release page and run it as a file, for example `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -AiMode local`.

```
irm https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

**Setting up the AI helper on this machine.** Add `-AiMode local` to the command-line installer to set up an AI model that runs on your own computer. The model needs no account and no key, and no text leaves the machine. Setup downloads the AI files only after it measures the hardware, and only when this computer can run the model. The result of that check does not change the rest of the install. In every case, setup installs Wheelhouse itself. When you choose the Parakeet speech engine, setup also installs the speech model. Setup then says where the model runs. When the model runs on the processor, it works but is slow. A short correction takes about 3 seconds, and a long correction takes about 12 seconds.

If a machine cannot run the model, setup tells you why and leaves the AI features switched off. Setup does not download several gigabytes that the machine could not run. `-AiMode cloud` selects the Google cloud model that the wizard offers. `-AiMode off` installs without AI. `-AiMode keep` leaves an existing AI configuration alone on a re-run.

**Command-line switches.** The first three switches below answer the three text prompts in advance, so the installer does not ask those questions. The three AI switches give the details of the cloud AI helper and work only with `-AiMode cloud`. To pass a switch, run the script as a file, as described above.

- `-SttProvider` chooses the speech engine: `parakeet_tdt` (Parakeet) or `google_stt` (Google Cloud). If you leave it out, the installer asks. If you press Enter, the installer keeps Parakeet on a new install. On an update, this answer keeps the current engine when that engine is Parakeet or Google Cloud. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -SttProvider google_stt`
- `-AutoStart` sets whether Wheelhouse starts when you log in: `yes` or `no`. If you leave it out, the installer asks, and the default answer is no. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -AutoStart yes`
- `-StartNow` sets whether Wheelhouse starts when the install finishes: `yes` or `no`. If you leave it out, the installer asks, and the default answer is no. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -StartNow yes`
- `-AiApiKey` gives the access key for the cloud AI helper. Setup reads it only together with `-AiMode cloud`, and it ignores the switch otherwise. Setup saves the key in the user environment variable WHEELHOUSE_AI_API_KEY, never in the settings file. It has no default. With `-AiMode cloud` and no key, setup leaves AI off, clears any saved key, and says so. Other programs on the computer can read a command line while the install runs. To keep the key off the command line, set the environment variable WHEELHOUSE_AI_API_KEY_INPUT to the key in the same PowerShell window. Then leave out `-AiApiKey`. The command is `$env:WHEELHOUSE_AI_API_KEY_INPUT = '<your key>'`. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -AiMode cloud -AiApiKey <your key>`
- `-AiBaseUrl` sets the address of the cloud AI service. Setup reads it only with `-AiMode cloud` and a key. The default is the Google address, `https://generativelanguage.googleapis.com/v1beta/openai/`. Another address must belong to an AI service that accepts requests in the same format as the OpenAI API. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -AiMode cloud -AiApiKey <your key> -AiBaseUrl https://example.com/v1/`
- `-AiModel` sets the name of the cloud AI model. Setup reads it only with `-AiMode cloud` and a key. The default is `gemini-2.5-flash-lite`. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -AiMode cloud -AiApiKey <your key> -AiModel gemini-2.5-flash`

Two more switches, `-Force` and `-KeepData`, apply only when you uninstall. See [Uninstalling Wheelhouse](#uninstalling-wheelhouse).

### What you need

- Windows 10 or Windows 11, 64-bit. Any Windows 11 edition works, and most Windows 10 editions work.
- 10 GB of free disk space.
- Memory (RAM): the offline speech engine, Parakeet, needs 8 GB. The Google Cloud speech engine needs 6 GB, which is the minimum for any engine. 16 GB is recommended. Below 8 GB, the installer stops for the offline engine but still installs the Google Cloud engine. In the setup wizard, choose Google Cloud at the speech-engine question. When you did not choose an engine in advance, the one-line command-line installer asks one question in place of the speech-engine question. The answer yes installs the Google Cloud engine, and the answer no stops the install. An update of an install that already uses the Google Cloud engine does not ask this question and keeps that engine. Below 6 GB, the installer stops for every speech engine, including the cloud one, and adding memory is the only fix.
- 4 or more CPU cores are recommended. With fewer cores, Wheelhouse installs and runs, but speech recognition can respond slowly.
- A microphone. You can connect one after the install.
- A keyboard and a mouse, or a person who can use them for you. You cannot install Wheelhouse by voice. Wheelhouse does not run yet during the install, so you answer the questions of the installer with clicks and typing. Voice control starts after the first run.
- An internet connection during the install. After the install, the default speech engine works offline.

### Successful installation

The wizard shows its progress step by step, and the PowerShell installer shows the same steps as text. The installation is complete when the installer reaches the end and does not stop on an error. Wheelhouse then appears in the Start menu under W and as a desktop shortcut.

### Installation failure messages

The failure messages of the PowerShell installer contain no personal data. You can include them in a help request. The failure window of the wizard is the exception. It shows the full path of the setup log, which is in the Windows temporary folder under your account name. The Windows error text that it quotes can also contain your account name. Before you include anything from that window in a help request, remove your account name from it.

When the wizard stops, a window states what went wrong and what to try. When setup knows the path of its log, the same window names the file. When that file exists, the window in most cases also offers to open it. When setup cannot work out the path, the window does not refer to a log. In both cases the window gives an address to write to, `help@wheelhouse-project.org`. The PowerShell installer prints the same two lines as text.

The common messages are:

- **"Wheelhouse appears to be running"** (during an update): the installer refuses to replace an application that is running. First, exit Wheelhouse. Right-click the floating button or the tray icon, which both open the same menu, and choose Exit. Then run the installer again. If the installer reports that it could not check, restart the computer and try again.
- **"Another program is using the Wheelhouse folder, so the update cannot replace it. Nothing was deleted."** (during an update): a program has a file or folder open inside the Wheelhouse application folder. An example is a command prompt or an editor that Wheelhouse opened. The installer changed nothing. Close those programs, or restart Windows. Then run the update again.
- **"This computer has N GB of memory"**: your machine has less memory than the chosen speech engine needs. When the message says the offline speech engine needs about 8 GB, install with the Google Cloud engine instead. In the setup wizard, choose Google Cloud. Or run install-wheelhouse.ps1 as a file with `-SttProvider google_stt`. For the offline engine, adding memory is the only fix. When the message says Wheelhouse needs about 6 GB for any speech engine, no choice of engine helps. In that case, adding memory is the only fix.
- **"Not enough free disk space"**: the message names the drive that holds your user data folder. Make sure that the drive has at least 10 GB free. Then run the installer again.
- **"Putting the speech model in place failed"**: the step that assembles the model stopped. The message includes the exit code of the step and the last lines that it printed. Run the installer again. The downloads stay on the computer and do not repeat. If the step fails the same way twice, include the message in a help request.
- **"Could not install uv"**: the cause is usually a blocked network. For example, corporate proxies can block the download. Use https://docs.astral.sh/uv/getting-started/installation/ to install uv manually. Then run the installer again.
- **"... failed its integrity check"**: the downloaded file does not match its published fingerprint. The most common cause is an antivirus program or a proxy that rewrites downloads. The other cause is a changed release asset. Add an exception or try a different network. If the failure continues, file an issue on the GitHub page.
- **"Downloading ... failed twice"**: the cause is a network problem. Run the installer again. The downloads continue from the place where they stopped.
- **"Setting up services\wheelhouse failed"** (or another folder under services): the installer could not build a Python environment. If the message shows a "uv sync exit code", the cause is usually a network or proxy problem. Check the connection and run the installer again. If the message says a path "is missing or is not a folder", the unpacked files are incomplete, or the antivirus program put them in quarantine. Run the installer again and check whether the antivirus program removes files.
- **"An incomplete speech model was found"**: this message is informational. It is not an error. The speech model that an earlier run installed is now incomplete or damaged. For example, one of its files no longer exists. The installer removes the model and installs it again. This repeats the 2.5 GB download. An interrupted run does not cause this message. The installer puts a model in place only after it checks that the model is complete.
- **"The spoken-hint vocabulary ..."**: this message is informational, not an error, and the install finishes. The word list that Parakeet reads before it can favour a boosted word is missing or failed its checksum, or the installer could not copy it. Parakeet biasing is off by default, so nothing else changes. If you switch biasing on, Parakeet applies no hints, and the word "boost" appears in the text as a plain word. Google still applies hints. When you run the installer again, it replaces the file.
- **"The Microsoft Visual C++ runtime is installed. Restart the computer before you use speech."**: this message is informational, not an error, and the install finishes. The Microsoft installer needs a restart before Windows uses the new runtime. Restart the computer. Then start Wheelhouse.
- **"You declined the Windows permission prompt, so the Microsoft Visual C++ runtime the speech engine needs was not installed ..."**: the install finishes, but speech can fail to start. Run the installer again. Select Yes when Windows asks.
- **"Wheelhouse could not download a Microsoft-signed copy of the Visual C++ runtime ..."**: the install finishes, but speech can fail to start. Either the download failed, or the downloaded file did not carry a valid Microsoft signature and setup discarded it. Run the installer again, or install the Microsoft Visual C++ Redistributable (x64) from Microsoft. Then start Wheelhouse again.
- **"Installing the Microsoft Visual C++ runtime the speech engine needs failed (code N) ..."**: the install finishes, but speech can fail to start. Run the installer again, or install the Microsoft Visual C++ Redistributable (x64) from Microsoft. Then start Wheelhouse again. The code is the exit code of the Microsoft installer, or the word "unknown" when setup could not read a code. Include the code in a help request.
- **No Wheelhouse entry in the Start menu**: first, check Start > All apps under W. New entries are not pinned to the front page. If the entry is absent, the desktop shortcut works the same way. The installer log records a "Shortcut created" line or a "Could not create" line. Include that line in a help request.

**Re-running the installer is safe at any time.** It repairs a broken install, continues interrupted downloads, and updates an existing install. It preserves your user data. The list of preserved data is under [Updating Wheelhouse](#updating-wheelhouse).

<!-- install-doc:end -->

If none of these apply, the Wheelhouse Assistant can read an installer message and identify the cause. See [Getting Help](wheelhouse_help.md#getting-help). You can also file a report at https://github.com/wheelhouse-project/Wheelhouse or send it to `help@wheelhouse-project.org`. Include the output of the installer or the setup log.

<!-- install-doc:start -->

### Updating Wheelhouse

There is no separate update procedure: **updating is re-running the installer.** Download the newest Wheelhouse-Setup.exe and run it, or run the same PowerShell line. Both are in [Running the installer](#running-the-installer). Each Wheelhouse-Setup.exe installs the release that it belongs to. A Wheelhouse-Setup.exe that you downloaded earlier installs that earlier release again. The newest Wheelhouse-Setup.exe and the PowerShell line install the newest release, because the PowerShell line downloads the newest installer script.

When the installer finds Wheelhouse already present, it updates Wheelhouse in place. First, exit Wheelhouse. Right-click the floating button or the tray icon, which both open the same menu, and choose Exit. The installer refuses to replace an application that is running.

Wheelhouse also checks once a day whether a newer release exists. When a newer release exists, a notice offers to update. "Update now" downloads the installer, checks it, closes Wheelhouse, and runs it. "Later" hides the notice for that version. Wheelhouse can show the notice again 7 days after the notice last appeared. A newer version shows its notice at once.

The setting `CHECK_FOR_UPDATES` in config.toml turns the automatic check off. A manual check does not depend on that setting. To start a manual check, choose **Check for updates** in the About Wheelhouse window, or say "check for updates". The spoken command opens the window if the window is closed. A manual check runs also when `CHECK_FOR_UPDATES` is false and during the 7-day "Later" wait. It sends the same one request as the automatic check.

A manual check does not change the time of the last check or the record of the last notice. The automatic schedule and the "Later" wait do not change. The window shows the result. The result is `Wheelhouse <version> is available` with an **Update now** button that works as the button in the notice, `You have the newest version (<installed version>).`, or `Wheelhouse could not reach GitHub. Check your internet connection and try again.` A copy of Wheelhouse that has no version number cannot compare versions. The window then names the newest release and says so.

An update replaces the application and preserves user data:

- The settings file (config.toml)
- Personal voice patterns
- Approved and declined dictation targets, and the retry counts for targets with no decision yet
- Three marker files that record that Wheelhouse already showed a one-time message. The files are for the Assistant explanation window (help_explainer_notebook_shown.toml), the first-click hint (click_first_use_hint_shown.toml), and the notice that Wheelhouse shows on the first start after an install (first_run_notice_shown.toml)
- Saved speech hints
- The record of the last update check, and the record of the version and the time of the last notice that Wheelhouse showed (update_check_state.toml)
- The downloaded speech model. It stays outside the part that an update replaces, so the 2.5 GB download does not repeat.

In the kept settings file, the update changes the assistant setting under [ai.help]. Release 1.2.1 and earlier named the assistant address setting gem_url. The update renames that setting to assistant_url. When assistant_url holds an address that release 1.2.0 or earlier shipped as the default, the update replaces it with the current Wheelhouse Assistant address. An address that you entered yourself stays. If the installer cannot make the change, it writes a warning, keeps the old address, and finishes the update.

The update also copies one value from the old settings file of the Parakeet engine (`services/stt_providers/sherpa_offline_parakeet_stt_server/config.toml`) into the new file. The value is `lead_word_path`, which turns the training word on. The rest of that file comes from the new release. An empty or missing value leaves the new file as shipped. The recordings are outside the part that an update replaces. The Speech Engines section describes the training word under "Your training word".

**If an update is interrupted** -- the installer preserves user files. A power cut, a closed window, or a crash can interrupt an update. Before the installer replaces anything, it copies the user files into a holding folder next to the application. The next run restores whatever it finds there. To recover, run the same command again. No manual step is required.

An update first moves the old application folder to a new folder beside it. The name of the new folder is `app.old-` followed by the date and time. If the update stops with an error before the installer restores your files, the installer puts the previous install back. Wheelhouse then starts as it did before the update. If the installer cannot put it back, its message names the folder that holds the previous install. Do not delete that folder. Run the update again, and the update restores your settings and voice patterns.

After a successful update, the installer deletes the old folder. If the installer cannot delete it, the setup wizard names the folder on its final page. Wheelhouse does not use that folder, and you can delete it. The installer tries again each time that it runs.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Security warnings

The project's author, David Chesley Hite III, digitally signs the Wheelhouse installer. The signature allows Windows to verify that the download came from the project and has no changes. Windows can still show a warning for each new release until Windows sees that file often enough. The source code is public at https://github.com/wheelhouse-project/Wheelhouse.

- **SmartScreen ("Windows protected your PC")**: this screen appears when you run a recently released Wheelhouse-Setup.exe. Click "More info". Check that the publisher name is David Chesley Hite III. Then click "Run anyway". If the setup wizard fails, its failure window names the setup log when the wizard can find the file. In most cases the window also offers to open the file. The file is at `%TEMP%\Setup Log <date> #<number>.txt`. Attach it to a help request.
- **Antivirus flags or rewrites the download**: some antivirus products quarantine downloads or alter them as they arrive. The installer verifies its own downloads against published fingerprints. These downloads are the application, the speech model, and the AI files. The installer refuses any altered download (the "failed its integrity check" message). Two downloads do not have this check. uv arrives through winget or uv's own installer. Setup downloads the Microsoft Visual C++ runtime from Microsoft only when the computer needs it. Setup keeps the file only when it carries a valid Microsoft signature. Add an exception for Wheelhouse, or install on a different network. Then run the installer again.
- **A downloaded script will not run**: Windows marks a saved install-wheelhouse.ps1 as a file from the internet, and PowerShell can refuse to run it. Remove the mark once with `Unblock-File .\install-wheelhouse.ps1`. Or start the script with `powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1`.

If you install from source, you do not see these warnings. CONTRIBUTING.md in the GitHub repository has the setup steps for development.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Uninstalling Wheelhouse

If you installed with Wheelhouse-Setup.exe, uninstall Wheelhouse like any Windows program: Settings > Apps > Installed apps > Wheelhouse > Uninstall. If you installed with the PowerShell one-liner instead, you need the script as a file. Download install-wheelhouse.ps1 from the releases page. Open PowerShell in the folder that holds the downloaded file. Then run:

```
powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall
```

The uninstaller does not run while Wheelhouse runs. First exit Wheelhouse: right-click the floating button or the tray icon and choose Exit. When you run the uninstaller from the command line as above, it asks two questions before it removes anything:

1. **"Remove Wheelhouse from this computer?"** -- the uninstaller removes nothing until you answer yes.
2. **"Keep your personal data?"** -- the personal data is the settings file, the voice patterns, and the downloaded speech model. If you keep personal data, the uninstaller also keeps the other eight files that an update preserves. These are the three dictation-target files, the three marker files, the saved speech hints, and the record of the last update check.

When you remove Wheelhouse through Windows after a Setup.exe install, the wizard asks only the second question. Windows already asked whether to uninstall. The wizard asks you the keep-or-remove question and then runs the same uninstaller. It does not repeat the first question.

Two switches answer those questions in advance. Both work only together with `-Uninstall`, and neither takes a value. If you leave out a switch, the uninstaller asks the question. Neither switch skips the check that Wheelhouse is not running.

- `-Force` skips both questions and removes everything, including personal data, unless you also give `-KeepData`. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall -Force`
- `-KeepData` answers the second question with keep. Without `-Force`, the uninstaller still asks the first question. Example: `powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall -Force -KeepData` removes Wheelhouse and keeps your personal data. It asks no question.

What each answer does:

- **Keeping personal data:** the uninstaller removes the application, all its shortcuts, and its bookkeeping folder. It also removes the `engine-audio` folder. That folder holds the sound files that the `SAVE_ENGINE_AUDIO` setting saves. These files contain everything you said, and only a running speech engine would otherwise delete them. The speech model remains in `%LOCALAPPDATA%\Wheelhouse`. Two more items remain there when they exist: the `lead-word` folder, which holds the training-word recordings of the Parakeet engine, and the file `stt_model_overrides.toml`. After a reinstall the training word is off even so, because the setting that turns it on is in the settings file of the Parakeet engine, inside the removed application. To turn it on again with the kept recording, select Turn on in the Your training word window. The uninstaller gathers the ten files that an update preserves into a subfolder there named preserved-user-data. These files are the settings file, the personal voice patterns, the three dictation-target files, the three marker files, the saved speech hints, and the record of the last update check. On a machine where the local AI helper is set up, the AI model and the program that runs it also remain there. They use several gigabytes of disk space. A folder whose name is `app.old-` followed by a date and time also remains there when an update could not delete it. Wheelhouse does not use that folder, and you can delete it. A later reinstall starts from defaults. To restore the previous settings and patterns, copy files back from the subfolder that holds the preserved files.
- **Keeping nothing:** the uninstaller removes the entire `%LOCALAPPDATA%\Wheelhouse` folder, the `%APPDATA%\Wheelhouse` folder, and all shortcuts (Start menu, desktop, and the start-at-login entry). It also clears a configured cloud AI access key from the user environment.

Those two folders, plus a small `WheelhouseSetup` folder that the uninstaller of the graphical installer uses, hold everything that Wheelhouse itself stores. Setup writes in up to five further places. It removes the shortcuts that it created and the start-at-login entry. When you choose to remove your personal data too, it also removes the saved cloud AI key. That key is the `WHEELHOUSE_AI_API_KEY` variable in your user environment, which setup writes only when you choose cloud AI. A removal that keeps your personal data also keeps that key, because the kept settings file can still turn cloud AI on.

Setup leaves the other places deliberately. It leaves uv, the environment manager that setup installed in the user profile, together with the entry for the uv folder that setup adds to your user PATH when uv's own installer did not add it. Where setup installed the Microsoft Visual C++ runtime, it leaves the runtime too. Other programs can also use either one. The graphical installer additionally leaves its own log in the Windows temporary folder.

`%APPDATA%\Wheelhouse` holds no personal data. It holds only bookkeeping, such as helper-process ID files, and the uninstaller removes it under either answer. When you run the uninstaller from the command line, it prints both folder paths when it finishes. When you remove Wheelhouse through Windows, the uninstaller runs hidden, so those paths never reach the screen. A removal through Windows shows a message only when something went wrong. Several different messages are possible.

Three of the messages concern the saved AI key. Two of them say that setup could not clear the key, or that setup could not read the output of the removal step at all. Both of those messages ask you to check WHEELHOUSE_AI_API_KEY yourself in Windows Environment Variables. The third message says that the key changed but setup could not tell the running programs. Its remedy is different: sign out of Windows and back in once.

Two further messages report a removal that could not run as intended. Setup stops with "Wheelhouse could not be fully removed" when the removal step fails, most often because Wheelhouse still runs. In that case, close Wheelhouse and run the uninstall again. Setup finishes but asks you to delete the `%LOCALAPPDATA%\Wheelhouse` folder by hand when the removal helper itself is missing.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Administrator windows and UAC prompts

Wheelhouse installs for a single user account and runs without administrator rights. A program without administrator rights cannot change system files or settings. Everything that the program types or clicks stays within what that user account can do.

One Windows rule follows from this. Windows does not allow a program to send keystrokes or clicks into a program that runs as administrator. Windows applies that rule to all non-administrator software. The rule has two consequences:

- **Programs running as administrator.** A program that you start with "Run as administrator" cannot receive typed text, key presses, or clicks from Wheelhouse. A program that elevates itself, as some system tools do, also cannot receive them.
- **UAC prompts.** The dimmed "Do you want to allow this app to make changes to your device?" screen has even stronger protection. Windows shows it on a separate secure desktop that no ordinary program can reach or observe.

**Observed behavior:** Wheelhouse detects dictation into an administrator window before it sends any keystroke. A notice titled "Wheelhouse can't type into administrator apps" appears in the corner of the screen. If you repeat the same attempt within a minute, no second notice appears. Wheelhouse types nothing in either case. The same notice appears for a terminal that runs as administrator.

Click commands that you speak with the safety word produce their own notice. Wheelhouse cannot see the contents of a protected window, so "x-ray click cancel" reports no match. Spoken key presses such as "press enter" produce no notice, because Windows discards them silently.

**Available options:**

- To dictate into administrator programs, start Wheelhouse as administrator. First exit Wheelhouse. Then right-click its Start menu entry and choose "Run as administrator".
- Use the physical keyboard and mouse for the administrator window or the UAC prompt. Use voice for everything else.
- If the program does not require administrator rights, start it normally. Wheelhouse can then type into it as it does any other program. Some tools require administrator rights and do not run without them. For those tools, use the two options above.

No Wheelhouse setting removes this limit. Windows enforces it, and the UAC screen remains protected in all cases.

<!-- install-doc:end -->

### First run

When Wheelhouse starts, it starts five programs. **The launcher** starts from the shortcut. It watches the logic, input, and GUI processes. When one of them exits, the launcher shuts all three down. It starts Wheelhouse again only when you choose Restart Wheelhouse from the menu.

**The logic process** interprets recognized speech and sends it to an action. **The input process** types text, presses keys, and performs clicks. **The GUI process** shows the tray icon and the floating status button. **The speech engine** is a separate helper that converts audio to text. If the speech engine stops, Wheelhouse restarts it once automatically. A second stop needs the Restart Wheelhouse menu item.

Within a few seconds the Wheelhouse icon appears in the system tray, near the clock. If it does not, see [Troubleshooting](wheelhouse_help.md#troubleshooting).

When the speech engine is ready, a window titled "Welcome to Wheelhouse" opens. It says that the round red button shows that Wheelhouse hears you. It also says that the word "help", when you say it on its own, opens the help page. The window has the buttons **Open help** and **Close**. Wheelhouse shows this window one time. It shows the window on the first start after an install, or on the first start after an update from a release that did not have this window.

### Microphone verification

Confirm that Windows itself receives audio before you diagnose recognition problems. Do three checks in this order:

1. **The privacy setting.** Open Settings > Privacy and security > Microphone. The setting "Let desktop apps access your microphone" must be on. When it is off, Windows refuses the microphone to Wheelhouse, and Wheelhouse shows this message:

   > WheelHouse cannot use your microphone. Windows is blocking it. Open Settings > Privacy and security > Microphone, turn on "Let desktop apps access your microphone", then start WheelHouse again.

   The message appears in two places. A Windows notification appears near the clock and closes itself after about three seconds. A Wheelhouse notice titled "Windows is blocking your microphone" appears in the corner of the screen and stays there until you close it.

2. **The input meter.** Right-click the taskbar speaker icon. Choose Sound settings. Scroll to Input. Make sure that the microphone you intend to use is selected. The level meter must move when you speak. If the meter stays flat, select a different input device.
3. **A dictation test.** Open Notepad. Confirm that Wheelhouse is listening. Say "hello world". On current hardware the words appear within about two seconds.

### The safety word ("x-ray")

The safety word protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands run only when the utterance begins with "x-ray". Without the safety word, "close window" is transcribed as ordinary dictation. With the safety word, "x-ray close window" closes the active window.

Common commands such as "undo", "copy", and "select all" need no safety word. Throughout this document, each command that requires the safety word has the "x-ray" prefix. The Voice Commands section of the help guide (the User guide page on the website) marks every command that requires it.

### The wake word ("computer")

Whenever listening is off, saying "computer" turns it back on. You need no keyboard or mouse. This holds whatever switched listening off: the floating button, the "stop listening" command, a start with listening off, Sonos playback, or an idle pause. There are two exceptions. The first exception is sound that this computer plays and that keeps listening paused. In that case the wake word does nothing in toggle mode and in push-to-talk mode, because the sound itself can say "computer". To turn listening on while the sound plays, click the floating button or the tray icon. In push-to-talk mode, hold the floating button instead. [Interaction Modes](wheelhouse_help.md#interaction-modes) tells when a hold listens over the sound.

The second exception is a program that took sole control of the speaker. While the program has that control, listening stays paused, and the wake word does not work. That pause ends only when the program releases the speaker (see `ENABLE_AUDIO_SUPPRESSION` under [Plugins](wheelhouse_help.md#plugins)). In push-to-talk mode the wake word ends only the idle pause, and listening then waits for the next hold of the floating button.

After a period with no keyboard or mouse activity, Wheelhouse pauses listening. The measure is input, not silence, so a film that you watch without keyboard or mouse input triggers the pause. The wake word and the safety word differ: "computer" turns listening back on, and "x-ray" runs a protected command.

You can configure the wake-word behavior in the wake_word section of the settings file, and it is enabled by default. The idle pause comes from the Idle Monitor plugin ([Plugins](wheelhouse_help.md#plugins)), which is also enabled by default. When that plugin is disabled, listening does not pause when idle.

---

<!-- install-doc:start -->

## Speech Engines

### Account requirements

The default configuration requires no account. Wheelhouse ships with the **Parakeet** engine as its default. Parakeet runs on the local processor, works offline, costs nothing, and transmits no audio. The installer downloads its model, and the settings preselect it.

An account is required in one case: the **Google Cloud** speech engine, selected at the installer's speech-engine question. That engine processes speech on Google's servers. It requires a Google Cloud account and a one-time credentials setup. The account is free, and most personal use stays within Google's free tier. Google charges for use beyond it. The Google Cloud engine needs less memory than the offline engine.

On a computer with less than 8 GB of memory, the installer stops for the offline engine but still installs the Google Cloud engine. In the setup wizard, choose Google Cloud. When you did not choose an engine in advance, the one-line command-line installer asks one question. It installs the Google Cloud engine when the answer is yes. An update of an install that already uses the Google Cloud engine keeps that engine and asks no question. On a computer with less than 6 GB of memory, the installer stops before installing anything, for every engine, and adding memory is the only fix.

### Local and cloud engines compared

| Aspect | Local engine (Parakeet) | Cloud engine (Google Cloud) |
|---|---|---|
| Accuracy | Very good for everyday dictation and commands | Very good; may have an edge on unusual names and vocabulary |
| Latency | Depends on your computer's speed; about 1.5-2 seconds to the first word on modern hardware | Depends on your internet connection, not your computer |
| Privacy | Audio never leaves your machine | Audio streams to Google's servers while you dictate |
| Cost | Free | Free tier, then Google charges for use beyond it |
| Account needed | None | A Google Cloud account and a one-time credentials setup |
| Works offline | Yes | No |

### Setting up Google Cloud credentials (only if you chose that engine)

This section applies only if you selected the **Google Cloud** speech engine at the installer's speech-engine question. With the default Parakeet engine, skip this section. That engine requires no account and no credentials.

If you selected Google Cloud, the installer ended with a warning that the engine requires credentials before it can transcribe. The installer also referred you to "the Google Cloud section". This is that section.

1. Create a Google Cloud account and a project at https://console.cloud.google.com/.
2. In the project, enable the Cloud Speech-to-Text API.
3. Create a service account (under IAM & Admin > Service Accounts). Give the account the Cloud Speech Client role.
4. Create a JSON key for that service account. A small file downloads.
5. Move the file somewhere permanent on your computer.
6. Open the settings file, `%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\config.toml`, in Notepad. Find the `[stt.google]` section. Put the full path to your key file in `credentials_file`, between the quotation marks. Write each backslash twice, because TOML reads a single backslash as an escape character:

```
[stt.google]
credentials_file = "C:\\Users\\yourname\\keys\\wheelhouse-speech.json"
```

7. Save the file. Restart Wheelhouse. The engine reads the key when it starts.

There is a second method. It does not need you to edit a file. Set an environment variable named GOOGLE_APPLICATION_CREDENTIALS to the full path of the key file. To do this, press the Windows key, type "environment variables", and open "Edit environment variables for your account". Then close Wheelhouse and start it again from the Start menu.

The menu item "Restart Wheelhouse" is not enough for this method. Part of the program continues to run during the restart, and that part still holds the environment from before you set the variable. Google's own software reads that variable automatically. Wheelhouse uses the variable whenever `credentials_file` is empty, so set one or the other, not both.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Adding or switching engines later

To switch between engines that you already set up on this computer, right-click either the floating button or the tray icon. Both open the same menu. Open **Speech-to-Text Provider**. Select the engine: **Google Cloud** or **Parakeet V3 (Local)**. The change takes effect at once.

Wheelhouse stops the running engine and starts the engine that you chose. Then Wheelhouse records the choice as last_provider in the stt section of the settings file, so the next start uses that engine. Wheelhouse records the choice as soon as the process of the new engine starts. If that process cannot be started at all, the choice is not recorded and the next start returns to the previous engine. If the process starts and the engine then fails its own startup, the choice stays recorded, and the next start tries that engine again. To change it, select a working engine from the menu.

When you switch to Google Cloud this way, Wheelhouse does not set up its credentials. See the Google Cloud section above.

To add an engine that you never set up on this machine, run the installer again. Select that engine at its speech-engine question. The installer downloads and sets up what that engine requires.

You can also request Parakeet from the menu. When the installer turned Parakeet off because its speech model is missing, **Speech-to-Text Provider** lists Parakeet as **Parakeet V3 (Local) - model not installed**. If you select that item, or if you select Parakeet while its model is missing or incomplete, Wheelhouse keeps the current engine. It also shows the notice "Parakeet speech model not installed". The notice gives the download size, about 2.5 GB. If you select **Not now** or close the notice, nothing changes.

If you installed or last updated Wheelhouse with the setup wizard, the notice offers **Download now**. With this choice, Wheelhouse closes and the installer runs again with Parakeet chosen. The installer repeats its whole setup, and this takes several minutes. It downloads the model and starts Wheelhouse with Parakeet. If Wheelhouse does not close within two minutes, the installer does not start.

On a computer that uses the Google Cloud engine and has less than 8 GB of memory, Wheelhouse still closes. The installer stops at its memory check, before any download. It shows a message that the built-in offline engine needs about 8 GB of memory. The installer downloads nothing and does not start Wheelhouse. Start Wheelhouse again from the Start menu.

When Download now is not possible, the notice offers **Copy command** instead. This copies the install command. Exit Wheelhouse. Run the command in PowerShell. Choose Parakeet at the speech-engine question. That command installs the latest release, which can be newer than the installed version.

The same re-run repairs a missing or incomplete speech model, for example after an interrupted download. The installer detects an incomplete model and installs it again. It is safe to run the installer again at any time.

The speech-engine question defaults to the engine that is already installed, so if you press Enter, the installer keeps that engine. When the installed engine is one that Wheelhouse no longer includes, the question defaults to Parakeet. If you install Parakeet in its place, the installer produces a message that the old engine was removed. The setup wizard shows this message on its final page. On the first start after the update, Wheelhouse shows the notice "Wheelhouse: Speech engine changed" one time. If you choose Google Cloud at the question instead, the installer installs Google Cloud with no such message.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Installer troubleshooting

**Installer failures**

The section [Installation failure messages](#installation-failure-messages) lists each installer failure message and its action. It is safe to run the installer again, and interrupted downloads continue.

<!-- install-doc:end -->

---

Generated: 2026-11-15 for the v1.4.4 release
Wheelhouse version: 1.4.4
