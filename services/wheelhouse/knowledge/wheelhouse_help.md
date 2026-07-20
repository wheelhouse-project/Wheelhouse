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
- For someone deciding whether to install: answer accurately from the "What Is
  Wheelhouse", "Can My Computer Handle Wheelhouse", "Speech Engines and
  Accounts", and "Getting Started" sections. Be candid about hardware limits
  and rough edges. Never oversell.
- For Wheelhouse-specific questions: answer ONLY from this document. Never
  invent features, commands, or settings not documented here.
- For general computing questions (microphone setup, Windows settings,
  PowerShell basics): help freely using your general knowledge.
- If the answer isn't in this document: "I don't have information about that
  feature. You can reach the developer at the Wheelhouse GitHub page:
  https://github.com/wheelhouse-project/WheelHouse (open an issue or start
  a discussion)."
- This document contains HTML comment lines such as <!-- install-doc:start -->.
  They are structural markers for tooling. Ignore them and never mention them.
- If an answer could depend on the Wheelhouse version (behavior that changed,
  download sizes, feature availability), tell the user which release this
  document describes -- read it from the "Generated" line in the footer at
  the very end ("for the vX.Y.Z release"). Ignore the footer's "WheelHouse
  version" line; it is an internal build identifier.
- When describing voice commands, always give an example of what to say.
- When a user seems overwhelmed, direct them to the "Day 1 Quick Start" section
  and tell them to ignore everything else until they're comfortable.
- When a user asks about hardware or performance, be direct about limitations.
  Don't promise it will work on every machine.
- Greet the user and ask what they need help with.

---

## What Is Wheelhouse

Wheelhouse gives you hands-free control of your Windows PC by voice. You speak, and your computer responds: dictate text into any application, issue commands, switch windows, launch programs, and click on-screen buttons by name -- all without touching the keyboard or mouse. If you use a Logitech MX-series mouse, Wheelhouse can also put screen brightness and volume on its thumb wheel.

**Who is it for?** Anyone who wants a faster, more natural, hands-free way to work. It is equally serious assistive technology: if using a keyboard and mouse is painful, difficult, or impossible, Wheelhouse aims to give you the whole computer by voice.

**What do you need?**
- A Windows 10 or Windows 11 PC (64-bit)
- A microphone (a headset or external microphone usually hears you better than a built-in laptop microphone, but start with whatever you have)
- About 10 GB of free disk space (the speech model is the biggest part)

That is the whole list. The installer brings everything else Wheelhouse needs, checks your hardware before it starts, and asks nothing of you afterward: no account to create, no subscription, and nothing extra to install first.

**Will my voice leave my computer?** No -- not unless you choose that. Out of the box, Wheelhouse turns your speech into text entirely on your own machine. No audio or text is sent anywhere, and there is no telemetry. Cloud speech recognition exists only as an option you would have to deliberately turn on.

**How it works, in one paragraph.** You speak into your microphone. Wheelhouse turns your words into text on your own computer, then decides what you meant. If it sounds like a command ("undo", "select all"), Wheelhouse carries it out immediately. Otherwise it treats your words as dictation and types them into whatever window you are working in -- a document, an email, a chat box -- with capitalization and spacing handled automatically. Punctuation is spoken: say "comma" or "period" and the symbol appears. Words show up as you talk, usually starting within about two seconds, and keep flowing while you speak instead of arriving all at once after you stop.

**Fair warning.** Wheelhouse is a young open-source project with a single primary author. Reliability is its first value and it is the author's daily driver, but it has been tested on a limited set of machines, so you may meet rough edges on hardware or applications it has not seen. If something fails for you, that report is wanted: https://github.com/wheelhouse-project/WheelHouse

---

## Day 1 Quick Start

**Stop here if you are new. Do these steps first, and ignore the rest of this document until they work.**

1. Download the installer and run it: https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/WheelHouse-Setup.exe
   If Windows shows "Windows protected your PC", click "More info", check that the publisher reads David Chesley Hite III, and click "Run anyway". The setup wizard's pre-selected answers are right for almost everyone. It downloads the speech model, so give it 10 to 20 minutes.
2. Start Wheelhouse from the Start menu or the desktop shortcut (the installer creates both).
3. Open Notepad.
4. Say **"hello world"** -- the words "hello world" appear.
5. Say **"new line"** -- the cursor moves to a new line.
6. Say **"undo"** -- the text is undone.
7. Say **"select all"** -- the text is highlighted.

**That's it -- you are using Wheelhouse.** Ignore everything else in this document until you feel comfortable with these basics.

One thing worth knowing on day 1: Wheelhouse starts in click-to-talk (toggle) mode -- click once to start listening, click again to stop. If you would rather hold a button down while you speak, see the "Interaction Modes" section.

---

## Pick Your Path

Once the basics work, pick the path that matches what you want to do next.

- **"I just want to dictate text into emails, documents, and chat."** Go to: Voice Commands (especially the dictation and punctuation subsections), then Speech Modes.
- **"I'm a programmer or power user and want everything Wheelhouse can do."** Go to: the full Voice Commands reference (commands, formatting, and navigation), then the Configuration section.
- **"I'm setting things up, or something isn't working."** Go to: Getting Started, then Configuration, then Troubleshooting.

---

## Getting Started (Full Version)

<!-- install-doc:start -->

### Installation

The installer is a normal Windows setup wizard. Download it and run it -- nothing needs to be installed ahead of time:

https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/WheelHouse-Setup.exe

If Windows shows a "Windows protected your PC" screen, see "Security warnings you may see" below. The whole process takes about 10 to 20 minutes, most of it downloading (roughly 1 GB in total). In plain language, the wizard:

1. Asks its questions up front: which speech engine you want (the pre-selected answer is right for almost everyone -- see Speech Engines and Accounts below), whether to set up the optional AI helper, whether Wheelhouse starts when you log in and right after setup finishes (both pre-selected and recommended), and whether Windows allows desktop apps to use your microphone.
2. Checks that your computer meets the requirements (see below) and tells you clearly if something is missing.
3. Installs uv, the environment manager Wheelhouse uses, into your user profile -- nothing system-wide.
4. Downloads the Wheelhouse application, verifies the download is genuine and undamaged, and sets up Wheelhouse's own private Python environments -- self-contained, they cannot interfere with anything else on your computer.
5. Downloads the offline speech model if you kept the default engine (about 650 MB -- the longest step).
6. Creates Start-menu and desktop shortcuts.

Wheelhouse installs for your user account only. No administrator rights are needed, and it does not touch other programs on your computer.

**Prefer a terminal?** The same install runs as one PowerShell line and asks the same questions as text prompts (pressing Enter accepts each default; the start-at-login prompt defaults to no):

```
irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

### What you need

- Windows 10 or 11, 64-bit (Windows 11 any edition; most Windows 10 editions work too)
- 10 GB of free disk space
- 8 GB of memory (RAM) -- a hard minimum; 16 GB is recommended. Below 8 GB the installer stops and cannot proceed with any speech engine, including the cloud one.
- 4 or more CPU cores recommended -- with fewer, Wheelhouse still installs, but speech recognition may respond slowly
- A microphone (you can plug one in after installing)
- An internet connection for the install itself; the default speech engine works fully offline after that

### What successful installation looks like

The wizard shows its progress step by step; the PowerShell installer reports the same steps as text. If it reached the end without stopping on an error, you are done. You will find Wheelhouse in the Start menu under W and as a desktop shortcut.

### What failure looks like

Every failure message the installer prints is designed to be understandable and safe to share. The common ones:

- **"Wheelhouse appears to be running"** (during an update): the installer refuses to replace an app that is running. Exit Wheelhouse first (right-click the tray icon, choose Exit), then run the installer again. If it says it could not even check, restart the computer and try again.
- **"This computer has N GB of memory"**: your machine is below the 8 GB minimum. This check stops the install for every speech engine, including the cloud one, so adding memory is the only fix.
- **"Not enough free disk space"**: free up 10 GB on the Windows drive and run the installer again.
- **"tar.exe was not found"**: only affects Windows 10 versions from before 2018, which lack the tool that unpacks the speech model. Install tar yourself, or choose the Google Cloud engine (which needs no model download).
- **"Could not install uv"**: usually a blocked network -- corporate proxies can block the download. Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and run the installer again.
- **"... failed its integrity check"**: the downloaded file does not match its published fingerprint. An antivirus or proxy rewriting downloads is the most common cause; a changed release asset is the other. Add an exception or try a different network, and if it keeps failing, file an issue on the GitHub page.
- **"Downloading ... failed twice"**: network trouble. Run the installer again -- downloads resume where they left off.
- **"Setting up services/... failed"**: a Python environment could not be built. If the message shows a "uv sync exit code", it is usually a network or proxy problem -- check the connection and run the installer again. If it says a path "is missing or is not a folder", the unpacked files are incomplete or were quarantined -- run the installer again and check whether antivirus is removing files.
- **"An incomplete speech model was found"**: informational, not an error. A previous unpacking was interrupted; the installer removes the incomplete files and unpacks again from the archive it already has. The 650 MB download only repeats if the archive itself is damaged.
- **No Wheelhouse entry in the Start menu**: check Start > All apps under W first -- new entries are not pinned to the front page. If it is truly absent, the desktop shortcut works the same; the installer log records a "Shortcut created" or "Could not create" line for a help request.

**Re-running the installer is always safe.** It repairs a broken install, resumes interrupted downloads, and updates an existing install while preserving your settings, your personal voice patterns, your approved dictation targets, your saved speech hints, and the downloaded speech model. When in doubt, re-run it.

<!-- install-doc:end -->

If none of that helps, ask for help at https://github.com/wheelhouse-project/WheelHouse -- paste the installer's output into your report.

<!-- install-doc:start -->

### Updating Wheelhouse

There is no separate update procedure: **updating IS re-running the installer.** Download and run the newest WheelHouse-Setup.exe (or run the same PowerShell line) from the Installation section. The installer always fetches the newest release, and when it finds Wheelhouse already on your computer, it updates it in place. Exit Wheelhouse first (right-click the Wheelhouse tray icon and choose Exit) -- the installer refuses to replace an app that is running.

An update replaces the application but keeps everything that is yours:

- Your settings (the config.toml file)
- Your personal voice patterns
- The dictation targets you have approved or declined
- Your saved speech hints
- The downloaded speech model -- it is stored outside the part an update replaces, so the roughly 650 MB download does not repeat

**If an update is interrupted** -- a power cut, a closed window, a crash -- your personal files are safe. Before replacing anything, the installer copies them into a holding folder next to the application, and the next run restores whatever it finds there. Recovery is running the same command again; nothing manual is needed.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Security warnings you may see

The Wheelhouse installer is digitally signed by the project's author, David Chesley Hite III, so Windows can verify the download came from the project unaltered. Windows may still warn you for a while after each new release, until it has seen the new file often enough. The complete source code is public at https://github.com/wheelhouse-project/WheelHouse, so anyone can inspect exactly what it does.

- **SmartScreen ("Windows protected your PC")**: can appear when you run a freshly released WheelHouse-Setup.exe. Click "More info", check that the publisher reads David Chesley Hite III, then click "Run anyway". If the setup wizard runs into trouble, it writes a log at `%TEMP%\Setup Log <date> #<number>.txt` -- paste that into a help request.
- **Antivirus flags or rewrites the download**: some antivirus products quarantine downloads or alter them as they arrive. The installer verifies every download against a published fingerprint and refuses anything altered (the "failed its integrity check" message). Add an exception for Wheelhouse, or install on a different network, then run the installer again.
- **A downloaded script will not run**: Windows marks a saved install-wheelhouse.ps1 as coming from the internet, and PowerShell may refuse to run it. Remove the mark once with `Unblock-File .\install-wheelhouse.ps1`, or start it with `powershell -ExecutionPolicy Bypass -File .\install-wheelhouse.ps1`.

If you would rather not click through security warnings, read the code and install from source: CONTRIBUTING.md in the GitHub repository has the development setup steps.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Uninstalling Wheelhouse

If you installed with WheelHouse-Setup.exe, uninstall it like any Windows program: Settings > Apps > Installed apps > WheelHouse > Uninstall. If you installed with the PowerShell one-liner instead, you need the script as an actual file: download install-wheelhouse.ps1 from the releases page, open PowerShell in that folder, and run:

```
powershell -ExecutionPolicy Bypass -File install-wheelhouse.ps1 -Uninstall
```

The uninstaller will not run while Wheelhouse is running -- exit it first (right-click the Wheelhouse tray icon and choose Exit). It then asks two questions before touching anything:

1. **"Remove Wheelhouse from this computer?"** -- nothing is removed until you answer yes.
2. **"Keep your personal data?"** -- meaning your settings, your voice patterns, and the downloaded speech model.

What each answer does:

- **If you keep your personal data:** the application, all its shortcuts, and its technical bookkeeping folder are removed, but your settings, your personal voice patterns, and the speech model stay behind in `%LOCALAPPDATA%\WheelHouse` (the settings and patterns are gathered into a subfolder there named preserved-user-data). If you reinstall later, the installer starts fresh -- copy files back from that folder if you want your old settings and patterns again.
- **If you keep nothing:** everything is removed -- the entire `%LOCALAPPDATA%\WheelHouse` folder and the `%APPDATA%\WheelHouse` folder, plus all shortcuts (Start menu, desktop, and the start-at-login entry). If you had set up a cloud AI access key, the uninstaller also clears it from your user environment.

For the privacy-minded: those two folders are the only places Wheelhouse lives, and `%APPDATA%\WheelHouse` never holds personal data (only technical bookkeeping such as helper-process ID files) -- it is removed either way. When the uninstaller finishes, it prints both folder paths so you can check for leftovers yourself.

<!-- install-doc:end -->

<!-- install-doc:start -->

### When Wheelhouse cannot type: administrator windows and UAC prompts

Wheelhouse installs for your user account only and runs without administrator rights. That is deliberate, and it is good for your safety: a program with no administrator power cannot change system files or settings, and nothing it types or clicks on your behalf can go further than your own account is allowed to go.

The trade-off is one Windows rule you will occasionally run into. Windows does not allow a normal program to send keystrokes or clicks into a program that is running as administrator. This is a protection built into Windows itself -- it stops any non-administrator software, not just Wheelhouse, from pressing buttons in privileged windows. In practice it means two things:

- **Programs running as administrator.** If you started a program with "Run as administrator" (or it elevated itself, as some system tools do), Wheelhouse cannot type into it, press keys in it, or click its buttons.
- **UAC prompts.** The dimmed "Do you want to allow this app to make changes to your device?" screen is even more protected: Windows shows it on a separate secure desktop that no ordinary program can reach or even see.

**What it looks like:** for dictation and keystroke commands, nothing -- you speak and the words have no effect in that window, with no error message. Click commands do show a notice: Wheelhouse cannot see inside the protected window, so "click cancel" reports no match. If Wheelhouse suddenly seems to have stopped working, check whether the window you are in is running as administrator. Click into any normal window (Notepad, your browser) and Wheelhouse works there immediately, because Wheelhouse itself never stopped -- only that one window was out of reach.

**What to do:**

- Use your physical keyboard and mouse for the administrator window or the UAC prompt, then go back to voice for everything else.
- If the program does not actually need administrator rights, start it the normal way (without "Run as administrator"). Wheelhouse can then type into it like any other program. Some tools genuinely require administrator rights and will not run unelevated -- for those, keyboard and mouse are the answer.

No Wheelhouse setting can lift this limit. It is enforced by Windows, not by Wheelhouse, and it protects you against far worse than a missed dictation.

<!-- install-doc:end -->

### First run

When you start Wheelhouse, several separate programs come up together as a team: **the launcher** (the piece you actually started -- it supervises the others and restarts them if one crashes), **the logic process** (the brain: decides what your speech means and routes it to the right action), **the input process** (the fingers: types text, presses keys, and clicks for you), **the GUI process** (the tray icon and the small floating status button), and **the speech engine** (its own helper program, turning your voice into text). Within a few seconds you should see the Wheelhouse icon in the system tray (the area near the clock). If it does not appear, see Troubleshooting.

### Microphone verification

Before judging Wheelhouse, make sure Windows itself can hear your microphone. Three quick checks, in order:

1. **The privacy setting first.** Open Settings > Privacy and security > Microphone, and make sure "Let desktop apps access your microphone" is on. This one switch silently blocks everything if it is off.
2. **The input meter.** Right-click the taskbar speaker icon, choose Sound settings, and scroll to Input: your microphone should be selected, and the level meter should bounce when you speak. If it stays flat, pick a different input device or microphone.
3. **The Notepad test.** Open Notepad, make sure Wheelhouse is listening, and say "hello world". On a modern computer the words should appear within about two seconds.

### The hotword ("x-ray")

Some commands could do real damage if they fired by accident while you were dictating a sentence -- closing a window, for example. Wheelhouse protects those commands with a hotword: they only run when the utterance starts with the word "x-ray". Say "close window" and nothing dangerous happens -- the words are ordinary dictation; say "x-ray close window" and the active window closes. Harmless everyday commands like "undo", "copy", and "select all" do not need the hotword; the Voice Commands section marks the ones that do.

### The wake word ("computer")

If you are quiet for a while, Wheelhouse can pause its listening to save effort. Saying "computer" wakes it back up -- no keyboard or mouse needed, which matters for hands-free control. The wake word and the hotword are different things: "computer" resumes listening after an idle pause, while "x-ray" unlocks protected commands. Wake-word behavior can be tuned in the settings file (the wake_word section); it is on by default.

---

<!-- install-doc:start -->

## Speech Engines and Accounts

### Do I need a Google account? (Short answer: probably not)

Most users need no account of any kind. Wheelhouse ships with the **Parakeet** engine as its default: it runs entirely on your own computer, on the regular processor (CPU), works offline, costs nothing, and never sends your audio anywhere. The installer downloads its model for you, and it is preselected in your settings from the start.

The one situation where an account comes up: you picked the **Google Cloud** speech engine at the installer's speech-engine question. That engine processes your speech on Google's servers and needs a free Google Cloud account plus a one-time credentials setup (Google charges for heavy use beyond its free tier, but most personal use stays within it). One caveat: on a computer with less than 8 GB of memory, the installer stops before installing anything -- its closing message mentions the cloud engine, but the installer cannot yet set it up on such a machine, so the fix is adding memory or using another computer.

There is also a third option for computers with an NVIDIA graphics card that has at least 4 GB of dedicated memory: **Distil-Whisper**, which runs locally on the graphics card. The installer offers it only when it detects a suitable card. It downloads its own model the first time it starts, so the first launch takes a few minutes.

### Local versus cloud, compared

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
7. Restart Wheelhouse if it is running.

That GOOGLE_APPLICATION_CREDENTIALS variable is where Wheelhouse expects to find the file: Google's own software reads it automatically, so there is nothing to edit inside Wheelhouse itself.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Adding or switching engines later

To switch between engines that are already set up on this computer, use the system tray: right-click the Wheelhouse icon, open **STT Provider**, and pick the engine you want. Wheelhouse remembers your choice (it is stored as last_provider in the stt section of the settings file) and uses it the next time it starts. If you switch to Google Cloud this way, remember that it cannot hear you until its credentials are set up -- see the Google Cloud section above.

To add an engine that was never set up on this machine, re-run the installer and pick that engine at its speech-engine question; it downloads and sets up whatever the engine needs. For example, if you originally chose Google Cloud and now want Parakeet, the re-run is what downloads Parakeet's speech model -- picking it from the tray menu is not enough on its own. The Distil-Whisper engine is always added this way: the installer sets it up only when you choose it, and offers it only on a computer with a suitable NVIDIA graphics card.

The same re-run is the repair path when the speech model is missing or incomplete -- for example when its download was skipped or interrupted the first time. The installer notices an incomplete model and reinstalls it. Re-running the installer is always safe, and on a re-run the speech-engine question defaults to the engine you already have, so pressing Enter keeps it (if your current engine is no longer available on this computer's hardware, the installer says so before asking).

<!-- install-doc:end -->

---

## Can My Computer Handle Wheelhouse?

A straight answer, because nothing is worse than installing something and finding it unusable.

### Minimum

- Windows 10 or Windows 11, 64-bit
- A dual-core processor -- Wheelhouse will install and run, but speech recognition may respond slowly; 4 or more cores is the comfortable floor
- 8 GB of RAM -- a hard minimum; below it, the installer stops and cannot proceed with any speech engine, including the cloud one
- 10 GB of free disk space
- An SSD is strongly recommended -- on an old spinning hard drive, startup and first responses are noticeably slower
- A working microphone

### Recommended

- 16 GB of RAM
- A modern quad-core or better processor (roughly, anything sold in the last six or seven years)
- A dedicated microphone -- almost any plug-in USB microphone beats a typical built-in laptop microphone, and recognition accuracy improves with it

### Do I need a graphics card (GPU)?

No. The default engine runs entirely on your regular processor and works well on modern CPUs. A graphics card is never required.

Where a GPU helps most is on a machine whose processor is older or slower: if you have an NVIDIA card with at least 4 GB of dedicated memory, the installer offers the Distil-Whisper engine, which moves speech recognition onto the card and takes the load off the CPU. Only NVIDIA cards can do this; on AMD or Intel graphics, use the default CPU engine or the cloud engine.

### The "will this be miserable?" checklist

- **If your computer runs Chrome with a handful of tabs and a Zoom call at the same time without struggling, Wheelhouse should work fine.** That is the practical baseline.
- If your computer already feels sluggish at basic tasks -- slow window switching, laggy typing in the browser -- expect noticeable delays in Wheelhouse too. It will still work; it will just feel slow.
- The real test is to try it: installation is free, safe to re-run, and easy to uninstall. If dictated words regularly take 3-4 seconds or more to appear, switch to a different engine (see Speech Engines and Accounts) rather than giving up.

### What speed to expect

- **Modern hardware, default engine:** roughly 1.5 to 2 seconds from speaking to the first word appearing, and then words flow in continuously as you keep talking -- you do not wait for the whole sentence.
- **NVIDIA GPU engine:** similar, sometimes slightly faster.
- **Older or slower CPUs:** 3 to 5 seconds or more to the first word is possible. That is slow enough to feel interrupted; the cloud engine is usually the better experience on such machines, since it shifts the work off your computer.

### Tips for slow machines

- Close heavy programs while dictating -- browsers with dozens of tabs, video editors, and games compete for the same processor the speech engine needs.
- Consider the Google Cloud engine. It does almost no work on your machine, so even a weak computer gets fast, accurate recognition -- the trade-offs are the account setup, the privacy difference, and needing an internet connection.
- Turn off features you do not use. If you have no Sonos speakers or Sony Bravia TV, leave those plugins disabled (they are off by default) so nothing extra runs in the background.
- Slightly slower machines sometimes trigger commands before you finish speaking; the speech timing values in the settings file can be increased to compensate (see Configuration).

## Voice Commands

Wheelhouse turns what you say into keystrokes, text, and system actions. Most commands work without any prefix, but some powerful or destructive commands require the hotword **"x-ray"** first so they cannot fire accidentally while you dictate; those are shown with an "x-ray" prefix in the tables below.

Two kinds of voice patterns exist: **commands** do something (press a key, switch a window, click a button) and are mostly spoken as their own utterance -- say the command, then pause -- while **replacements** work **inline during dictation**: say them mid-sentence and Wheelhouse swaps the spoken word for a symbol or corrected text as it types. All of the punctuation words ("period", "comma", "question mark") work this way -- you never stop dictating to punctuate.

### Top 10 Commands to Learn First

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

### Daily Workflow Examples

**Example 1 -- Writing and cleaning up an email**

1. Dictate the body of the message normally. Sprinkle punctuation as you go: "hi team comma new paragraph the release is ready period"
2. Notice a typo two characters back: say **"backspace 2"** to rub out the last two characters, then re-dictate.
3. Finished the draft but the tone is rough: select the paragraph with **"select paragraph"**, then say **"x-ray fix"** to send it to the configured AI server for grammar and flow cleanup.
4. Happy with the result: say **"x-ray activate outlook"** (or whatever your email app is called) to bring the mail window forward, and **"submit"** when you are ready to send with Enter.

**Example 2 -- Researching something you copied**

1. Highlight a phrase on screen with the mouse (or select it by voice with "select word").
2. Say **"copy"** to grab it.
3. Say **"x-ray browser"** to bring your web browser forward.
4. Say **"paste"** into the address bar, then **"submit"** to press Enter.

### Full Voice Command Reference

#### Dictation Control

These commands control what Wheelhouse types and give you escape hatches when a word you want to dictate collides with a command.

| Say this | What happens | Notes |
|---|---|---|
| literal [words] | Types the words after "literal" exactly, skipping all command and replacement processing | The escape hatch -- see the detailed explanation in "Special Commands" below |
| insert [text] | Inserts raw text with no capitalization, spacing, or formatting applied | Useful for exact fragments like an email address or a product code |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item 1", "item 5" |
| submit | Presses Enter | Also works as the last word of a sentence: "hello world submit" types "hello world" and then presses Enter. To type the word itself, say "literal submit" |

One background protection worth knowing about: utterances beginning with "okay Google", "ok Google", or "hey Google" are silently discarded, so talking to a nearby voice assistant while Wheelhouse is listening is not typed into your document.

#### Text Editing

**Deleting and correcting**

| Say this | What happens | Notes |
|---|---|---|
| backspace [number] | Deletes one character to the left, or that many with a number | e.g. "backspace 5"; the number is optional, counts capped at 50 |
| delete [number] | Deletes one character (or that many) to the right | e.g. "delete 5"; counts capped at 50 |
| delete word | Deletes the entire word under the cursor | |
| undo [number] | Undoes the last action, or several | Ctrl+Z; e.g. "undo 3" |
| redo [number] | Redoes the last undone action, or several | Ctrl+Y |

Wheelhouse also accepts common mishearings of "undo" and "redo" ("undue", "undu", "redu"), so the command still fires when the recognizer gets the spelling wrong.

**Line breaks, indenting, and keys**

| Say this | What happens | Notes |
|---|---|---|
| new line | Inserts a line break without submitting the field | Works inline during dictation |
| new paragraph | Inserts two line breaks | Works inline during dictation |
| tab [number] | Presses Tab that many times | e.g. "tab 3"; "indent 3" does the same. The number is required -- "tab" alone is typed as the word |
| shift tab | Outdents (Shift+Tab) | "outdent" does the same |
| escape | Presses the Escape key | |
| press [keys] | Presses any key or key combination by name | e.g. "press enter", "press alt f4", "press f5" -- see the detailed subsection below |

**Clipboard**

| Say this | What happens | Notes |
|---|---|---|
| copy / copy line / copy all | Copies the selection, the entire current line, or everything | |
| copy screen | Starts the Windows screenshot snipping tool | |
| x-ray cut | Cuts the current selection | Requires the hotword for safety |
| paste | Pastes the clipboard contents | |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires the hotword |

**Selection**

| Say this | What happens |
|---|---|
| select all / select word / select line / select paragraph | Selects everything in the current field, or the word, line, or paragraph under the cursor |

**Saving, finding, and searching**

| Say this | What happens | Notes |
|---|---|---|
| x-ray save | Saves the current document (Ctrl+S) | |
| x-ray find [text] | Opens the app's find bar and types the search term | e.g. "x-ray find invoice" |
| x-ray replace | Opens find-and-replace (Ctrl+H) | |
| x-ray search | Copies the current selection and runs a web search for it | Select the text first |

##### The "press [keys]" Command in Detail

"press [keys]" is the generic escape hatch for any keyboard shortcut. Modifiers are automatically held down first regardless of the order you say them -- so "press delete control" is equivalent to "press control delete". If any word in the phrase is unrecognized, Wheelhouse presses nothing and discards the phrase; it is not typed as text. If your speech engine hyphenates a token (hearing "f-11" or "control-alt"), Wheelhouse untangles that automatically.

**Modifier keys you can say**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: f1 through f12.

**Letters**: any single letter a through z. Example: "press control shift t".

**Digits**: a digit works only when another key name follows it. Avoid ending the phrase with a digit -- a trailing digit is read as a repeat count, so "press control 2" presses Ctrl twice instead of Ctrl+2.

**Symbols by spoken name**: reliably pressable -- backtick, semicolon, slash (forward slash), backslash (back slash), comma, period (dot), single quote (apostrophe), left/right bracket (open/close bracket), equals (equal), minus (hyphen, dash), right parenthesis (close paren). Other symbol names are unreliable in "press": shifted symbols (colon, tilde, pipe, question mark, double quote, braces, less than, greater than, plus, underscore, left parenthesis) come out as the wrong character, and hash, at, ampersand, asterisk, caret, percent, dollar, and exclamation press nothing. To type any of these characters, dictate them as punctuation words instead (see Punctuation and Symbols below) -- that path handles every symbol correctly.

**Examples**: "press control shift t", "press f5", "press alt f4", "press windows d", "press left bracket".

#### Text Formatting

All of these apply to whatever text is currently selected. Select first (with the mouse or with "select word" / "select line"), then say the command.

**Case and shape transforms**

| Say this | What happens |
|---|---|
| uppercase / lowercase | Converts the selection to UPPERCASE or lowercase |
| capitalize | Capitalizes the first letter and lowercases the rest |
| title case | Converts the selection to Title Case |
| snake case / camel case / pascal case / kebab case | Converts the selection to that programming style: snake_case, camelCase, PascalCase, or kebab-case |
| compress | Removes the spaces, joining the words together ("hello world" becomes "helloworld") |

**Rich text styling**

| Say this | What happens | Notes |
|---|---|---|
| x-ray bold text / x-ray italics / x-ray underline | Bolds, italicizes, or underlines the selection (Ctrl+B / Ctrl+I / Ctrl+U) | Works in apps that support rich text |

**Wrapping**

These wrap your selection in the chosen characters. Said with no selection, they insert an empty pair and drop your cursor between the two characters -- handy while dictating code.

| Say this | What happens |
|---|---|
| parentheses [text] | Wraps the selection in ( ), inserts an empty ( ) pair, or inserts the spoken text wrapped ("parentheses hello" gives "(hello)") |
| brackets | Wraps in [ ] |
| braces | Wraps in { } |
| angle brackets | Wraps in < > |
| quotes | Wraps in double quotes |
| single quotes | Wraps in single quotes |

Note: words spoken after a wrapping word in the same breath are wrapped verbatim -- symbol words like "colon" inside the wrapped text are typed literally, not converted.

#### Navigation

The "go" and "grab" commands move the cursor without touching the keyboard. "go" moves; "grab" moves while selecting along the way. You can chain several moves in one utterance with "then". The utterance must start with "go" -- "grab" works only as a step chained after a "go" move (for example "go home then grab to end"). Said on its own, "grab ..." is typed as dictation.

| Say this | What happens |
|---|---|
| go home | Jumps to the start of the line |
| go end | Jumps to the end of the line |
| go top | Jumps to the top of the document |
| go bottom | Jumps to the bottom of the document |
| go left / go right [count] | Moves one character, or a count ("go right 5") |
| go left / go right [count] words / paragraphs | Moves by words or paragraphs ("go right 3 words", "go left 2 paragraphs") |
| go start of word / go end of word | Jumps to the start of the current word ("beginning of word" also works), or forward past it (in most apps the cursor lands at the start of the next word) |
| go start of paragraph / go end of paragraph | Jumps to the start of the current paragraph, or forward to the next one (landing just past the end of the current one in most apps) |
| go home then grab to end | Jumps to the line start, then selects to the line end ("go end then grab to home" selects the same span from the other side) |
| go home then grab right 3 words | Selects the first three words of the line |
| go top then grab to bottom | Selects the entire document |

Counts can be digits ("3") or spoken words ("one" through "ten"; digits work up to 50). "to", "too", and "for" are accepted as sound-alikes for 2 and 4, so a recognizer that hears "go right to words" still moves two words. If any part of a "go" utterance cannot be understood, the whole phrase is typed as dictation instead -- garbled speech never produces surprise cursor movement.

#### Punctuation and Symbols

These are replacements: they work **inline during dictation** -- say the word as part of your sentence and Wheelhouse types the symbol in its place, no pause needed.

| Say this | You get |
|---|---|
| period | . |
| comma | , |
| colon | : |
| semicolon | ; |
| question mark | ? |
| exclamation point (or exclamation mark) | ! |
| apostrophe | ' |
| hyphen | - |
| dash | an em dash (the long dash) |
| slash | / |
| backslash | \ |
| backtick | ` |
| at sign | @ |
| hashtag | # |
| dollar sign | $ |
| percent | % |
| caret sign | ^ (also fires if heard as "carrot sign") |
| ampersand (or "and sign") | & |
| asterisk | * |
| underscore | _ |
| plus sign | + |
| equal sign | = |
| tilde | ~ |
| vertical bar | the pipe character |
| ellipsis | ... |
| space bar | a single literal space |

Two mishear tolerances ship built in, because the default local engine often mishears the spoken words "comma" and "colon": saying **"colin"** as an entire utterance inserts ":", and saying **"come"**, **"kama"**, **"commer"**, or **"come on"** as an entire utterance inserts ",". Inside a longer sentence these words dictate normally -- the tolerance applies only when the word is the whole utterance. To type one of them as a standalone word, use the escape hatch: "literal come" / "literal colin".

If the recognizer routinely mishears another word -- a name heard as a sound-alike, say -- teach Wheelhouse a personal correction in the Pattern Manager ("x-ray patterns"); it then applies inline during dictation like the built-in punctuation words.

#### Application Switching

| Say this | What happens | Notes |
|---|---|---|
| x-ray activate [app name] | Brings the named application's window forward if it is already running (a spoken name does not start a closed app; only .exe targets in custom patterns are launched when not found) | e.g. "x-ray activate outlook" |
| x-ray browser | Brings your default web browser to the front | Wheelhouse looks up which browser is your Windows default at the moment you speak |
| x-ray notepad | Brings Notepad to the front | |

#### System

Window management and Windows itself.

| Say this | What happens | Notes |
|---|---|---|
| zoom in / zoom out | Zooms in or out (Ctrl and plus / minus) | |
| create tab | Sends Ctrl+N | New tab in most editors; note that in most browsers Ctrl+N opens a new window, not a tab |
| create window | Sends Ctrl+Shift+N | New window in editors; opens a private/incognito window in most browsers |
| x-ray close window | Closes the active window (Alt+F4) | Requires the hotword for safety |
| x-ray maximize / x-ray minimize | Maximizes or minimizes the active window | |
| x-ray desktop | Shows the desktop (Windows+D) | |
| Windows settings | Opens the Windows Settings app | Also fires if heard as "Window settings" |

#### Mouse Control

To be clear, because many voice packages advertise this: **this release has no voice commands that move the mouse pointer** (no "mouse up", no grid overlay for pointer positioning). What Wheelhouse offers instead usually covers the need: clicking controls by saying their name or number (Voice Element Clicking, the next section) is faster and more precise than steering a pointer by voice, and volume and screen brightness sit on the thumb wheel of a Logitech MX-series mouse (see Plugins) -- a deliberate choice for people who keep one hand on a mouse or trackball. If you need full pointer-by-voice control, pair Wheelhouse with your preferred pointer solution and let it handle dictation, commands, and clicking by name.

#### Voice Element Clicking

Wheelhouse can click buttons, links, menu items, and other on-screen controls for you. There are two ways to pick a control: by its **name**, or by putting a **number** on every clickable control and saying the number. The numbered overlay is the answer for controls that have no obvious name to say (icon-only toolbar buttons, for example) or when several controls share the same name.

**Clicking by name**

Say "click", then the name of the control -- optionally with "the" in front (ignored) and a role word at the end (narrows the search to that kind of control). The "x-ray" hotword prefix is optional on all of the clicking commands: "click cancel" and "x-ray click cancel" both work.

| Say this | What happens |
|---|---|
| click cancel | Clicks the control named "cancel" |
| click the submit button | Clicks the button named "submit" |
| click the home link | Clicks the link named "home" |
| click the file menu | Clicks the menu named "file" |
| click remember me checkbox | Clicks the check box named "remember me" |

**Role words** you can add after the name: **button**, **link** (a hyperlink), **menu** (a menu item), **tab**, **checkbox** (or **check box**), and **box** / **field** / **input** (a text entry field).

If you say no role word, Wheelhouse matches any clickable control by name. A role word said on its own with no name (for example "click button") is treated as the name, not a role -- it looks for a control literally named "button".

**The numbered overlay**

| Say this | What happens | Notes |
|---|---|---|
| apply numbers | Paints a number on every clickable control in the front window | Numbers stay up until you dismiss them |
| click 3 | Clicks the control labelled 3 | Say any visible number |
| dismiss numbers | Removes the numbers | |

Things worth knowing about the overlay: the numbers **stay on screen** until you say "dismiss numbers" -- clicking a numbered control refreshes them in place so you can pick another, and they follow whichever window is in front. When a "click [name]" matches more than one control well, the numbers appear by themselves on just those finalists so you can pick by number. While numbers are showing, saying a number always picks the numbered label -- so a control whose real name is a digit (a calculator "7", for instance) cannot be reached by name until you say "dismiss numbers" first. And if the numbers look out of place after a page scrolls or swaps content, say "apply numbers" again to repaint them.

**What you see for each outcome**

A successful click shows no notice -- the control is clicked. Failures show a brief advisory notice near the tray so you know why nothing happened: **not found** ("No match for [name]" -- nothing matched; try the numbered overlay), **ambiguous** (the numbered overlay opens on the finalists so you can pick by number; the "Found [A] and [B] -- be more specific" notice appears only when the overlay cannot open), and **could not complete the click** (the wording names the reason -- the control is disabled, the click timed out, or the overlay went stale and needs reapplying). Notices are rate-limited, so a burst of failed attempts will not bury your screen in messages.

#### Wheelhouse Control

Commands that steer Wheelhouse itself: listening modes, help, personal patterns, and the AI features.

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening: Wheelhouse listens only while you hold the floating button | A notification confirms the switch |
| click to talk mode | Switches back to toggle listening (click to start, click to stop) -- the default | |
| x-ray wheelhouse help online | Opens the hosted Wheelhouse help page in your browser | Requires the online help URL to be configured (the gem_url setting under [ai.help]); if it is not set, Wheelhouse says out loud that online help is not configured |
| x-ray patterns | Opens the Pattern Manager | "x-ray pattern manager" also works; see "Special Commands" below |
| x-ray fix | Sends the selected text to the configured AI server for grammar and polish, then replaces the selection with the corrected version | Requires the AI server to be configured and reachable; Wheelhouse speaks its progress ("Correcting", "Done") and always preserves your original text on any failure |
| x-ray cancel fix | Cancels an in-progress fix | |
| x-ray boost | Adds the selected text to the speech recognition hints | See "Special Commands" below -- on the default engine this saves the hint but does not apply it until you opt in |

Turning the microphone on and off is not itself a voice command -- you click the floating microphone button or the tray icon (or, in push-to-talk mode, hold it). This is deliberate: a system that could be silenced by voice could also be silenced by a stray phrase.

About help: "wheelhouse help online" is the supported way to ask questions -- it opens the hosted help page in your browser. Wheelhouse also contains an in-app help chat window, but the in-app help chat is currently disabled in this release; the voice patterns that opened it are switched off. Text correction ("x-ray fix") is the live AI feature of this release.

### Special Commands with Extra Explanation

**"literal [words]"**

Say "literal" followed by whatever you want to type, and Wheelhouse inserts those exact words without running them through any command or replacement patterns. This is the escape hatch when you need to dictate a phrase that would otherwise trigger a command: "literal copy" types the word "copy" instead of copying, "literal period" types the word instead of a full stop, and "literal new line" types the phrase instead of inserting a line break.

"literal" takes effect wherever it appears in an utterance, not only as the first word: everything you say after "literal" is typed exactly as spoken, and the word "literal" itself is not typed. A sentence with "literal" in the middle therefore types the rest of that sentence verbatim, so use it only when you actually want the escape hatch. To type the word "literal" itself, say "literal literal".

**"x-ray boost"**

When the speech recognizer keeps mishearing a specific word -- usually a name, a product, a place, or a technical term -- select the problem word anywhere on screen (highlight it with the mouse or say "select word") and say **"x-ray boost"**. Wheelhouse copies the selection and sends it to your speech engine as a new recognition hint, saved to a shared hints file on disk so it **persists across restarts** -- you only need to boost each tricky word once. Hints are capped at 100 characters, so boost individual words or short phrases, not whole sentences.

One important distinction: **saving a hint and applying it are two different things.** **Parakeet (the default engine) saves the hint but does NOT apply it out of the box**: hint biasing ships turned off because applying hints slows recognition by roughly 25 percent per utterance in the project's measurements. To make Parakeet actually use your saved hints, set enabled = true under the [hotwords] section of the Parakeet engine's own config file and restart Wheelhouse -- accepting the slower recognition. Until you opt in, do not expect boosting to change what Parakeet hears. **Distil-Whisper** and **Google Cloud Speech-to-Text** both apply saved hints out of the box (as decoder biasing terms and speech adaptation phrases, respectively).

**"x-ray patterns" (the Pattern Manager)**

This opens the **Pattern Manager** window, a browsable interface that lists every voice command and text replacement Wheelhouse knows. The list groups patterns by category; selecting any entry shows its details -- the trigger phrase, what it does, and whether it needs the hotword.

From the Pattern Manager you can **view** any pattern (including every built-in), **create** new personal patterns (a shortcut that types your email address, a correction for a word the engine keeps mishearing, a command that opens a program), **edit** and **delete** the ones you created, **customize** a built-in (this makes a personal copy with the same trigger that overrides it; the built-in is never modified, so deleting your copy restores stock behavior), and **change the command hotword** (the "x-ray" prefix) if another word works better for your voice.

Your personal patterns are stored in a separate per-machine file, so they survive Wheelhouse upgrades, and the shipped patterns file is never touched.

**"x-ray wheelhouse help online"**

Opens the hosted Wheelhouse help page in your default browser, where you can ask questions in plain language. It requires the online help URL (the gem_url setting in the [ai.help] section of the configuration) to be set; with no URL configured, Wheelhouse answers out loud that online help is not configured. This is the supported help path -- the in-app help chat window is currently disabled.

## Speech Modes

A common worry with voice control software is that you will have to constantly announce "command mode" or "dictation mode" and that everything falls apart when you forget. Wheelhouse does not work that way. You never switch modes by hand. Wheelhouse decides on the fly, word by word, whether you are giving it a command or dictating text -- and the rule it uses is simple enough that it quickly becomes second nature.

### The three things that can happen to your words

- **Command**: Wheelhouse recognizes what you said as a voice command and performs it. You say "undo" and it presses the undo shortcut. You say "delete five" and it deletes five characters. Nothing gets typed.
- **Dictation**: Wheelhouse types what you said into whatever text field you are working in. You say "dear Sarah thank you for the update" and those words appear in your email.
- **Inline replacement**: certain words get swapped for symbols or corrected spellings even in the middle of dictation. You say "hello comma world" and you get "hello, world" -- the word "comma" becomes the punctuation mark instead of being typed out.

### How Wheelhouse decides: position determines intent

The position of a word in your phrase is what tells Wheelhouse what you meant:

- **The first word of a phrase is a potential command.** When you start speaking after a pause, Wheelhouse checks whether your first word could begin a known command. If it could, Wheelhouse holds it very briefly (well under a second) to see whether the next word or two completes the command. Say "delete five" as its own phrase and the command runs. If the words turn out not to match any command after all, they are typed as ordinary text -- nothing is ever lost.
- **Words in the middle of a phrase are dictation.** Say "I want to delete five items" and the whole sentence is typed, including the word "delete". Because "delete" arrived mid-sentence, Wheelhouse knows you meant it as text, not as an instruction. This is why you can dictate naturally without tiptoeing around command words.
- **Replacement words work anywhere.** Words like "comma" and "period" are substituted whether they come first, last, or mid-sentence, because their whole job is to appear inside dictation.

### The hotword safety gate

Some commands could do real damage if they fired by accident while you were dictating -- closing a window, for example. Those commands are protected by a safety word: they only run when you say "x-ray" first, as in "x-ray close window". Everyday low-risk commands do not need it. And the hotword follows the same position rule as everything else: "x-ray" only has its special meaning as the very first word of a phrase. Mention x-ray machines in the middle of a sentence and the word is typed. If you say "x-ray" and what follows is not actually a command, the whole phrase (including "x-ray") is typed as text -- again, no words are ever lost.

### Words appear as you speak

Wheelhouse streams your speech. You do not talk, stop, and wait for a block of text to appear -- words show up on screen while you are still talking, flowing out one after another. The only exception is that tiny hold at the start of a phrase while Wheelhouse checks whether you are giving a command, and a similar brief hold around replacement words; both are fractions of a second.

### Chaining cursor moves with "then"

You can chain cursor movements and text selections into one phrase by saying "then" between them:

- "go home then grab to end" -- jumps to the start of the line, then selects everything to the end of the line.
- "go top then grab to bottom" -- jumps to the top of the document, then selects everything to the bottom.

This chaining works only for the "go" (move the cursor) and "grab" (select text) navigation commands. Other commands -- copy, paste, switching windows, and so on -- are each spoken as their own separate phrase.

## Interaction Modes: Toggle vs Push-to-Talk

Speech modes (above) are about what Wheelhouse does with your words. Interaction modes control something more basic: when Wheelhouse listens at all. There are two, and you can switch between them at any time.

### Toggle mode (the default)

Wheelhouse listens continuously whenever speech is switched on. One click on the floating on-screen button -- or one left-click on the Wheelhouse icon in the system tray -- turns listening off; another click turns it back on. This is the mode for hands-free use: once listening is on, you never need to touch anything again.

A bonus even in toggle mode: press and hold the floating button (about a fifth of a second or longer) and Wheelhouse listens only for as long as you hold it, like a walkie-talkie, then goes back to normal when you release. Handy when you mostly keep listening off but want to speak one quick command.

### Push-to-talk mode

Wheelhouse listens only while you are physically holding down the floating button. Press and hold to talk; release and listening stops instantly. While you hold, Wheelhouse also mutes your computer's speakers so that sound from a video or music cannot leak into the microphone and be transcribed -- your volume is restored the moment you release. In this mode, a single left-click on the tray icon does nothing; the hold works on the floating button.

Two things worth knowing:

- **Safety release.** If a hold somehow gets stuck (say the release never registered), Wheelhouse automatically stops listening after 30 seconds and restores your audio, so you are never left with a live microphone or muted speakers. If you dictate long passages in this mode and the 30-second cutoff interrupts you, you can raise it with the ptt_safety_timeout_seconds setting in the [speech] section of the config file.
- Push-to-talk needs a hand on the mouse (or a finger on a touchscreen), so it trades away some of the hands-free benefit that is Wheelhouse's main point.

### How to switch between the modes

Any of these works, at any time:

- **By voice**: say "push to talk mode" to switch to push-to-talk, or "click to talk mode" to switch back to toggle mode.
- **Tray menu**: right-click the Wheelhouse icon in the system tray and click "Push-to-Talk Mode". A checkmark on that menu item shows when push-to-talk is active.
- **Double-click**: double-click the floating button or the tray icon to flip between the two modes.
- **At startup**: the interaction_mode setting in the [speech] section of the config file ("toggle" or "push_to_talk") sets which mode Wheelhouse starts in. The voice, menu, and double-click switches change it while Wheelhouse is running.

### Which should you use?

Stay with toggle mode if you want hands-free control -- it is the default for a reason, and it is the mode most people should use. Choose push-to-talk when you are in a noisy room, when other people's voices or your speakers keep getting transcribed, or when you use voice input only occasionally and want to be certain Wheelhouse hears nothing between holds.

## Configuration

You do not need to edit any settings to use Wheelhouse. Every value ships with a working default, and the most common choices (which speech engine to use, push-to-talk versus click-to-talk) can be changed from the tray menu or by voice without ever opening a file. This section exists for the day you want to fine-tune something.

Wheelhouse keeps its settings in a plain text file called config.toml, which you can open in Notepad. The installer creates it from a template; your copy is personal to your machine and never sent anywhere. Lines starting with a number sign are comments; the file explains many of its own settings inline.

A few practical notes before the reference:

- Change one thing at a time, then restart Wheelhouse so the change takes effect.
- If you make a mistake and something stops working, you can restore the defaults by copying the shipped template (config.toml.example, in the same folder) over your config.toml.
- Settings marked "device-specific" are off by default and only matter if you own that piece of hardware. Wheelhouse runs fine with all of them turned off.

### General Settings (top of the file)

**SPEECH_WEBSOCKET_HOST** (default 127.0.0.1, meaning "this computer only"): the internal address the speech engine uses to reach Wheelhouse. Change only for the advanced setup where speech recognition runs on a second computer on your home network.

**REPLACEMENT_TIMEOUT_MS** / **COMMAND_TIMEOUT_MS** (default 700 each, in milliseconds): how long Wheelhouse waits after you stop speaking before deciding a command or correction phrase is complete. Raise to 900-1000 if commands fire before you finish (common on slower machines); lower slightly if responses feel sluggish.

**GREEDY_TIMEOUT_MS** (default 5000): a longer wait for commands that intentionally keep listening for more words. Rarely needs changing.

**COMMAND_COMPLETION_WAIT_MS** (default 1000): a short pause after a command finishes so a fast follow-up does not collide with it. Raise on a slow machine if back-to-back commands step on each other.

**ENABLE_AUDIO_SUPPRESSION** / **ENABLE_SONOS_SUPPRESSION** / **ENABLE_IDLE_SUPPRESSION** (default all true): pause listening while computer audio or Sonos music is playing, or after the computer sits idle. Turn one off only if you want Wheelhouse listening during playback -- expect more misrecognitions, because the microphone picks up the audio.

**LOG_FILE** / **LOG_LEVEL** (defaults: empty, meaning the standard log location, and DEBUG): where the activity log goes and how detailed it is. Change only when a support conversation asks you to.

**LOG_TRANSCRIPTS** (default false): a privacy setting -- false keeps the words you dictate and your clipboard contents out of the log files (only text lengths are noted). Set true only while troubleshooting recognition, then turn it back off: while on, everything you dictate, including passwords, accumulates in the logs.

**SIDE_OFFSET** (default 10): width in pixels of the left-edge screen zone where the mouse thumb wheel adjusts brightness instead of volume. Raise it if the brightness zone is hard to hit.

**BRIGHTNESS_INCREMENT** / **VOLUME_INCREMENT** (defaults 1.0 / 0.5): the size of each thumb-wheel adjustment step. Raise for faster, coarser changes; lower for finer control.

**FLOATING_BUTTON_SIZE** / **FLOATING_BUTTON_POS** / **FLOATING_BUTTON_VISIBLE** (defaults 30 pixels, corner offset -18 -15, false = hidden): the small on-screen status button. Set FLOATING_BUTTON_VISIBLE to true for an always-visible microphone click target -- especially handy in push-to-talk mode.

**SPEECH_ENABLED_ON_STARTUP** (default true): whether Wheelhouse starts listening as soon as it launches. Set false to turn the microphone on manually each session.

**SHOW_SPEECH_PULSE** (default true): pulse the tray icon while Wheelhouse hears you -- a useful "yes, I can hear you" signal. Turn off only if the animation distracts.

**SPATIAL_SOUND_EXEC** / **SPATIAL_SOUND_FORMAT** (defaults: empty = feature off, "Dolby Atmos for home theater"): voice switching of Dolby Atmos spatial sound via a small free NirSoft helper tool. Fill in the tool path only if you use Dolby Atmos and have that tool installed; everyone else can ignore both.

### Brightness Coordinator ([brightness_coordinator])

Wheelhouse changes screen brightness in layers: real hardware brightness first (a supported TV or the laptop panel), then a software dimming effect once the hardware is as low as it goes. Most people never touch this section.

**software_dimmer** (default gamma_dimmer): the software dimming method -- "gamma_dimmer" (darkens through the graphics card), "overlay" (a translucent overlay window), or "flux" (drives a companion dimming app's hotkeys). Change only if dimming misbehaves with your monitor setup.

**unwinding_threshold** (default 10): currently has no effect -- Wheelhouse hands control back to the hardware only once software dimming is fully undone, whatever this is set to.

**flux_transition_percent** (default 2): percent of brightness per simulated hotkey press when driving a companion dimming app.

**flux_dim_hotkey** / **flux_brighten_hotkey** (defaults Alt+PageDown / Alt+PageUp): the shortcuts pressed to drive that companion app. Change only if you remapped the app's own hotkeys.

### Plugins ([plugins.*])

Every plugin has its own [plugins.*] section with an enabled switch. All of
them -- what each plugin does, every setting with its default, and
troubleshooting basics -- are covered in the Plugins section later in this
document.

### Wake Word ([wake_word])

After an idle pause, you can wake Wheelhouse by saying its wake word out loud -- no keyboard or mouse needed. This runs entirely on your computer.

**enabled** (default true): on/off. **keyword** (default "computer"): the wake word. **sensitivity** (default 0.5, range 0-1): lower it if saying "computer" often fails to wake Wheelhouse; raise it if ordinary conversation keeps waking it by accident. **mode** (default "idle_recovery"): what the wake word is used for -- waking Wheelhouse from an idle pause. **model_dir**: where the listening model lives on disk; set by the installer, do not change it.

### Text Insertion Fine-Tuning ([ui_actions.*])

These settings govern the mechanics of how dictated text lands in other programs. The defaults are tuned carefully; change them only when troubleshooting a specific symptom.

**Timing ([ui_actions.timing])** -- all in milliseconds unless noted; on older or heavily loaded machines, raising these can fix text that arrives garbled, half-pasted, or out of order: **clipboard_verification_timeout_ms** (default 250), **clipboard_operation_delay_ms** (default 50), **selection_clear_delay_ms** (default 20), **context_gather_delay_ms** (default 10), **post_paste_delay_ms** (default 30), and **utterance_clipboard_timeout_seconds** (default 60.0) -- how long, in seconds, a copied utterance stays available for the "paste that" style of command.

**Short-text typing ([ui_actions.verified_unicode])**: **max_chars** (default 50) -- dictations up to this length are typed directly, character by character, avoiding your clipboard; longer ones go through the clipboard. Lower it if a particular app mishandles direct typing; raise it to have more dictations bypass the clipboard.

**Browser recognition ([ui_actions.foreground_check])**: **same_process_browser_names** -- the web browsers Wheelhouse recognizes (browsers manage their windows in an unusual way); all the mainstream ones are already listed. **same_process_browser_names_extend** adds an unusual browser without retyping the built-ins.

**Dictation safety lists ([ui_actions.text_target])**: before typing anywhere, Wheelhouse checks that the focused spot really accepts text. The four extend settings (**allow_class_names_extend**, **deny_control_types_extend**, **deny_class_names_extend**, **browser_process_names_extend**, default all empty) extend the built-in allow and deny lists for an unusual app. Most people should use the built-in approval prompt instead -- when Wheelhouse is unsure about a text box, it asks on screen and remembers your answer.

### Speech Interaction ([speech])

**interaction_mode** (default "toggle"): "toggle" keeps the microphone on until you turn it off (click to start, click to stop); "push_to_talk" listens only while you hold the floating button, muting system audio during the hold (a single tray-icon click deliberately does nothing in that mode). You can also switch by voice ("push to talk mode" / "click to talk mode") without editing anything.

**ptt_safety_timeout_seconds** (default 30): in push-to-talk mode, automatically releases the microphone if a hold gets stuck. Raise it if you routinely dictate longer than 30 seconds in one hold.

**notify_on_revision** (default false): show a small notice when the speech engine revises its guess at what you said.

### Speech Recognition Engine ([stt])

**last_provider** (default "parakeet_tdt"): which speech-to-text engine Wheelhouse uses -- "parakeet_tdt" (local, offline, no account), "distil_medium_en" (a more accurate local engine that needs a recent graphics card), or "google_stt" (Google's cloud service; needs a Google Cloud account, sends audio to Google). You normally switch engines from the tray menu rather than editing this -- Wheelhouse writes your choice here for you, which is why it is called "last" provider.

**[stt.google] boost_words** (default empty): words or phrases the Google engine should favor when unsure -- useful for names or uncommon words it keeps getting wrong. Only matters on the Google engine.

**[stt.azure] subscription_key** / **region** (defaults empty / "eastus"): credentials for the Azure cloud speech option. Only matters if you deliberately set up Azure; most people never touch this.

### AI Features ([ai], [ai.server], [ai.help])

Wheelhouse's AI features are optional and off unless you point them at an AI server. In this release, the live AI feature is dictation text correction -- fixing up dictated text on request. The in-app help chat is currently disabled; these settings also gate it, but it will not appear regardless of what you set.

**[ai] enabled** (default true): the master switch for all AI features -- nothing happens unless a server address is also configured below. Today this means dictation text correction; it also gates the in-app help chat, which is currently disabled in this release.

**[ai] knowledge_base** (default: the shipped help document): the document the in-app help assistant would consult; because the in-app help chat is currently disabled, this setting has no effect today.

**[ai.server] base_url** (default http://localhost:11434/v1, a local Ollama server): the address of the AI server Wheelhouse talks to, using the standard OpenAI-style interface. Any OpenAI-compatible address works, local or hosted. Leave empty to turn AI off entirely.

**[ai.server] model** (default "gemma3:12b"): the model name to request from that server. Change it to whatever model your server has installed.

**[ai.server] kind** (default "local"): "local" or "cloud" -- whether the server is on your own machine or out on the internet, which frames the privacy tradeoff: with a local server, the text being corrected never leaves your computer. Spell "cloud" exactly: any other value is treated as local.

**API credential**: deliberately no key is stored in the config file. If your server needs one (a cloud service usually does; a local Ollama does not), set the WHEELHOUSE_AI_API_KEY environment variable instead (Windows Settings, search "environment variables", "Edit environment variables for your account", add the variable, restart Wheelhouse). That way the secret never sits in a settings file that could be copied or shared.

**[ai.server] timeout_s** (default 30): seconds Wheelhouse waits for the AI server before giving up on a request. Raise it if a slow local model keeps timing out.

**[ai.help] gem_url** (default empty): the web address that "wheelhouse help online" opens in your browser -- the help surface of the current release, since the in-app help chat is currently disabled. Until a page is configured, the command answers out loud that online help is not configured.

**[ai.help] max_response_tokens** (default 800): caps the length of an answer from the in-app help chat; because that chat is currently disabled, this setting has no effect today.

**If the AI server is unreachable**, nothing breaks: the AI features quietly turn themselves off, and dictation, voice commands, and everything else keep working exactly as before. AI is a convenience layered on top of Wheelhouse, never a requirement.

### Voice Clicking ([click])

Settings for the "click ..." commands that let you press buttons and links by naming them, and for the numbered overlay ("apply numbers", then "click 5"). The defaults work well; the ones a user might plausibly adjust:

**enabled** (default true): master switch for voice clicking. **min_confidence** (default 0.4) and **clear_winner_margin** (default 0.15): how sure Wheelhouse must be before clicking something by name, and how clearly one candidate must beat the runner-up -- raise min_confidence if it clicks the wrong thing, lower it if it too often finds no match; with no clear winner it shows the numbered overlay instead of guessing. **notice_max_names** (default 3): how many candidate names appear in the "did you mean" style notice. **overlay_badge_font_pt** (default 12): the size of the painted numbers -- raise it if they are hard to read. **response_timeout_ms** (default 3000) and **walk_deadline_ms** (default 2500): how long Wheelhouse searches a window for clickable things before giving up -- raise both on a slow machine if clicks time out in complex windows. **snapshot_ttl_seconds** (default 30): how long the numbered overlay's snapshot stays valid. **browser_processes** and **browser_processes_extend**: the browser-like apps (browsers, Slack, Discord, and similar) that need a deeper search -- add an app to the extend list if voice clicking cannot see controls inside it. **enable_screen_reader_flag** (default false): tells apps a screen reader is present, which makes some expose more clickable elements -- try true if an app hides its buttons; note some apps change their appearance when this is on.

The remaining click settings (tiebreaker distances, substring matching thresholds, fallback switches) are fine-tuning knobs best left at their defaults.

### Slow Machine Tweaks

If Wheelhouse feels laggy or unreliable on an older computer, these specific changes help, roughly in order of impact:

1. **Use the default speech engine.** "parakeet_tdt" ([stt] last_provider) is the lightest local engine and runs on any CPU; do not switch to "distil_medium_en" without a capable recent graphics card. If even the default struggles, "google_stt" moves the work to the cloud -- at the cost of an account and an internet connection.
2. **Give yourself more speaking time.** Raise REPLACEMENT_TIMEOUT_MS and COMMAND_TIMEOUT_MS from 700 to 900-1000, and COMMAND_COMPLETION_WAIT_MS from 1000 to 1500 if quick back-to-back commands collide.
3. **Slow down text insertion.** Under [ui_actions.timing], raise post_paste_delay_ms (30 to 60), clipboard_operation_delay_ms (50 to 100), and clipboard_verification_timeout_ms (250 to 500) if dictated text arrives incomplete or garbled.
4. **Give voice clicking more time.** Under [click], raise response_timeout_ms (3000 to 5000) and walk_deadline_ms (2500 to 4000) if clicks time out in complex windows.
5. **Be patient with a local AI server.** Raise [ai.server] timeout_s from 30 to 60 if corrections time out -- or leave AI off; nothing else depends on it.

### Speech Recognition Quality Tweaks

**The hallucination filter (Distil-Whisper engine only).** Whisper-family speech engines have a well-known quirk: fed a cough, a throat-clear, or background noise, they sometimes invent polite filler -- a stray "thank you" or "okay" you never said. The Distil-Whisper engine ships with a confidence filter that discards such low-confidence utterances instead of typing them. Its threshold is **hallucination_logprob_threshold** (default -0.55) in the Distil-Whisper provider's own config file, not the main config.toml. That default was calibrated on a single male voice with a studio microphone, so it may be too strict for other voices: if real speech is sometimes silently ignored -- more likely with a strong accent, quiet speech, or a laptop microphone -- lower it to -0.7 or -0.8. More negative means more permissive: fewer real words discarded, the occasional phantom "thank you" let through; a very large negative number turns the filter off entirely. If no threshold feels right for your voice, switch to the Google engine from the tray menu -- it handles noise and varied voices more robustly (a cloud service: needs an account, sends audio to Google). The filter does not apply to the default Parakeet engine, whose design neither produces the confidence signal it relies on nor shares the Whisper family's phantom-phrase quirk to the same degree.

**Boosting words the engine keeps missing.** If you use the Google engine and it consistently mishears a particular name or technical term, add that word to boost_words under [stt.google] to tip recognition in its favor.

## Plugins

Plugins are optional add-ons that connect Wheelhouse to extra hardware and services: your laptop screen, Sonos speakers, a Sony TV, and a few Windows features. Every plugin has its own `[plugins.*]` section in config.toml with an `enabled` switch, so you can turn each one on or off without deleting anything. You do not need any of them for dictation and voice commands to work, and a plugin whose hardware is missing or offline never breaks Wheelhouse -- it sits quietly and keeps retrying in the background.

Two of these plugins respond to the mouse thumb wheel -- the small horizontal wheel on the side of the mouse, under your thumb. This is not the main scroll wheel: that one keeps its normal scrolling job. Wheelhouse reads the thumb wheel directly from the device, which currently works only with Logitech MX-series mice. Screen zones pick what the thumb wheel controls: pointer at the left edge of the screen, it adjusts brightness; anywhere else, volume -- no command or click needed. Step size and zone width are adjustable in the configuration reference.

### Internal Panel

Controls the brightness of a laptop's built-in screen from the brightness scroll zone. Enable or disable with `plugins.internal_panel.enabled` (default: enabled). There are no other settings -- everything is detected automatically. It talks to the laptop display through a built-in Windows interface, entirely on your own machine. On a desktop PC with no built-in panel it does nothing and is safe to leave enabled.

### Sonos

Adjusts Sonos speaker volume from the volume scroll zone, and pauses Wheelhouse's listening while music is playing so song lyrics are not typed into your documents. Enable with `plugins.sonos.enabled` (default: disabled -- turn it on only if you own Sonos speakers). Settings:

- `polling_interval` -- how often, in seconds, to check whether music is playing (default 2).
- `speaker_ip` -- optional. Wheelhouse finds Sonos speakers on your network automatically; set this only if discovery fails or you want a specific speaker.
- `request_connect_timeout` / `request_read_timeout` -- advanced network timeouts (defaults 2.0 and 5.0 seconds); rarely need changing.

It connects to the speaker over your home network directly -- no Sonos account or internet service is involved. Sound coming from your computer or TV through the Sonos does not pause listening; only streamed music does.

### System Volume

Controls the normal Windows volume (the same one as the taskbar speaker icon) from the volume scroll zone, and quiets system audio while you hold the push-to-talk button. Enable with `plugins.system_volume.enabled` (default: enabled). Settings:

- `device_type` -- which audio device to control: `"default"` (the usual choice), `"communications"`, or a specific device name.
- `volume_step_db` -- loudness change per wheel step, in decibels (default 1.5).
- `min_volume_db` / `max_volume_db` -- the volume floor and ceiling (defaults -96.0 and 0.0).

Fully local, no network. Both volume plugins can stay enabled: at startup Wheelhouse picks one -- Sonos when your audio is actually playing through a Sonos, System Volume otherwise -- so they never fight.

### Bravia (Sony TV)

Brings a Sony Bravia TV used as a computer monitor into Wheelhouse's brightness control, so the brightness scroll zone can dim and brighten the TV itself. Enable with `plugins.bravia.enabled` (default: disabled). Settings:

- `ip_address` -- your TV's address on the home network. Optional: leave it blank and Wheelhouse searches the network for the TV automatically; set it if you have more than one TV or discovery fails.
- `psk` -- the pre-shared key you set on the TV under Settings -> Network -> Home Network -> IP Control. Required; the plugin will not start with it blank.
- `device_name` -- the TV's audio device name exactly as Windows shows it under Sound settings -> Output (default "SONY TV"). This is not a label you invent: Wheelhouse uses it to look the device up for spatial-sound handling, so it must match the Windows name exactly.

It connects to the TV over your home network using Sony's built-in remote-control interface. The plugin first checks whether a Sony display is physically connected; on a machine without one it goes quietly inactive, so leaving it configured on a laptop you travel with is harmless.

### Idle Monitor

Notices when you have stepped away (no keyboard or mouse activity) and pauses listening so Wheelhouse is not transcribing an empty room; listening resumes when you return or say the wake word. Enable with `plugins.idle_monitor.enabled` (default: enabled). Settings: `idle_timeout_minutes` (default 10) and `polling_interval_seconds` (default 4). Fully local -- it only asks Windows how long since your last keypress or mouse move. Almost everyone should leave this on.

### Window Positioning

Automatically moves the Windows On-Screen Keyboard out of the way when it would cover the window you are working in. Enable with `plugins.window_positioning.enabled` (default: enabled). Settings: `target_window_names` (which windows to move; default is the On-Screen Keyboard), `move_cooldown_seconds` (default 0.5, prevents jitter), `clearance_gap_pixels` (default 5), and `ignore_window_titles` / `ignore_window_classes` (windows that should never trigger a move). Fully local.

### A note on the software dimmer section

You may see a `[plugins.software_dimmer]` block in config.toml. It is a leftover -- Wheelhouse does not read it. The screen-dimming method is chosen by the `software_dimmer` key in the `[brightness_coordinator]` section instead, and the shipped default there works for most people.

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

### Plugin troubleshooting basics

- Confirm the plugin's `enabled = true` and restart Wheelhouse -- plugins are only discovered at startup.
- Check the log's startup lines: each plugin reports whether it initialized, went inactive (hardware not found), or failed, usually with the reason.
- For Sonos and Bravia, make sure the device is powered on and reachable from this PC on the same network.
- For Bravia specifically, IP Control must be enabled on the TV and the pre-shared key in config.toml must match the one set on the TV.
- If the mouse wheel does nothing, check the scroll zones: pointer on the left side of the screen adjusts brightness, anywhere else adjusts volume -- and at least one plugin for that control type must be enabled.

## Troubleshooting

Most problems have simple causes, and none mean your computer is broken or that you did something wrong. Work through the checklist first, then look up the matching entry below.

### First-Time Setup Checklist

Walk through these five checks in order. Stop at the first one that fails and jump to the entry it names.

1. **Did the installer finish without red error lines?** If not, see "Installer failures."
2. **Do Windows Sound settings show your microphone picking up sound?** Right-click the speaker icon on the taskbar -> Sound settings -> Input, then speak. Does the input meter move? If not, see "Microphone not detected."
3. **Is the Wheelhouse icon visible in the system tray?** If it is missing, see "Wheelhouse does not start or the tray icon is missing."
4. **Open Notepad, click in the empty page, and say "hello". Does the word appear?** If not, see "Dictation not appearing in text fields."
5. **Now say "undo". Does the word disappear?** If not, see "Commands not recognized."

If all five pass, Wheelhouse is working -- any remaining trouble is specific to one app or one feature, and the entries below cover the common cases.

### Common Problems

**Microphone not detected**

- *What you see:* Wheelhouse starts, but nothing happens when you speak, and Windows Sound settings show no input activity.
- *What is likely wrong:* Windows is using a different microphone than the one you are speaking into, or a privacy setting is blocking desktop apps from the microphone.
- *What to try:* Open Settings -> Privacy and security -> Microphone and make sure "Let desktop apps access your microphone" is on. Then open Sound settings -> Input and pick the microphone you actually use. Restart Wheelhouse afterward so it picks up the change.

**Wheelhouse does not start or the tray icon is missing**

- *What you see:* You start Wheelhouse and nothing appears, or the tray icon never shows up.
- *What is likely wrong:* One of Wheelhouse's background processes failed during startup -- most often because a speech model is missing or an earlier install was interrupted.
- *What to try:* Re-run the installer -- always safe, repairs a broken install, and keeps your settings and personal data. If it still will not start, restart the computer and try once more before reaching out for help.

**Speech engine not connecting**

- *What you see:* The tray icon shows the speech engine as disconnected, or Wheelhouse seems to be waiting forever for speech to start working.
- *What is likely wrong:* The speech engine failed to start. Common reasons: its model was never downloaded, the Google Cloud engine has no credentials, or the computer is low on memory.
- *What to try:* Switch engines from the tray menu -- Parakeet is the built-in offline engine and needs no account. If the engine you want was never fully set up, re-run the installer and choose it at the engine question. For the Google Cloud engine, check that the GOOGLE_APPLICATION_CREDENTIALS environment variable points at your credentials file (see Speech Engines and Accounts). If the engine will not start right after an install or update, open a NEW PowerShell window and run "uv --version" -- if that command is not found, the installer's tooling never made it onto your PATH; re-run the installer, which checks and repairs this.

**Commands not recognized**

- *What you see:* You say "maximize" and nothing happens, or the word appears as typed text instead.
- *What is likely wrong:* The speech engine misheard you (for example "maximum" instead of "maximize"), or you spoke while other audio was playing and the words ran together.
- *What to try:* Speak a little more deliberately, with a brief pause before the command. Do not raise your voice: louder speech makes recognition worse, not better -- normal conversational volume works best. If one particular word is misheard over and over, select a correctly spelled copy of it anywhere on screen and say "x-ray boost" to teach the speech engine to expect that word.

**Command words are typed as text instead of doing anything**

- *What you see:* You say "close window" and the words "close window" appear in your document instead of the window closing.
- *What is likely wrong:* Nothing is broken. Destructive commands need the safety word "x-ray" in front, so they can never fire by accident while you are dictating a normal sentence.
- *What to try:* Say "x-ray close window". The command list in this document marks which commands need the safety word.

**Dictation not appearing in text fields**

- *What you see:* You speak, Wheelhouse clearly hears you, but no text appears in the app you are looking at. You may see a small notice saying Wheelhouse was not sure the spot you are in accepts text.
- *What is likely wrong:* Either the text field is not actually focused (clicked into), or Wheelhouse could not confirm the focused spot is a real text box. The caution is deliberate: typing into the wrong place in some apps -- especially web browsers -- can trigger keyboard shortcuts instead of entering text, so Wheelhouse refuses rather than guesses.
- *What to try:* Click directly inside the text field and try again. If a notice appears with a "Try it anyway" button, use it -- once the text lands correctly a few times, Wheelhouse remembers that spot and stops asking. If Notepad works, Wheelhouse itself is fine and the problem is that one app's unusual text field.

**Real speech silently ignored, or short dictations disappear**

- *What you see:* With the Distil-Whisper (graphics card) engine, occasional short phrases -- or for some voices, quite a lot of real speech -- produce nothing at all, with no error.
- *What is likely wrong:* That engine's noise filter (tuned for a typical voice on a good microphone) can misjudge real speech as noise -- more likely with a strong accent, quiet speech, or a laptop's built-in microphone -- and silently drop it.
- *What to try:* Make the filter less strict: in the Distil-Whisper engine's own config file, lower hallucination_logprob_threshold from -0.55 to -0.7 or -0.8 (more negative means less strict), then restart Wheelhouse. If tuning does not help your voice, switch engines from the tray menu -- the Google Cloud engine does not use this filter.

**AI text correction does nothing or times out**

- *What you see:* Dictated text is not being cleaned up even though AI is turned on. (Text correction is the AI feature in this release; the in-app help chat is currently disabled.)
- *What is likely wrong:* Wheelhouse does not run the AI itself -- it sends requests to a separate AI server you point it at. If that server is missing, unreachable, slow, or does not have the requested model, the AI features quietly switch off while everything else keeps working.
- *What to try, in order:* confirm [ai] enabled = true and [ai.server] base_url is filled in (an empty base_url turns AI off on purpose); confirm the server is reachable at that address and the [ai.server] model name is one it really offers; raise [ai.server] timeout_s if the server is just slow to answer; and for a remote server that requires a key, set the WHEELHOUSE_AI_API_KEY environment variable (the key never lives in the settings file) and restart Wheelhouse.
- *Reassurance:* An unreachable AI server never breaks Wheelhouse. Dictation, voice commands, and everything else keep working with AI off.

<!-- install-doc:start -->

### Installer troubleshooting

**Installer failures**

The installer's failure messages -- low memory, low disk space, a blocked uv download, an integrity-check failure, an interrupted download, a failed services setup, an incomplete speech model, or Wheelhouse still running during an update -- are explained in the "What failure looks like" part of the Getting Started section, along with what to do about each. The short version: re-running the installer is always safe, downloads resume where they left off, and every message is safe to paste into a help request.

<!-- install-doc:end -->

---

## Getting Help

If the answer is not in this document, email help@wheelhouse-project.org or reach the developer at the Wheelhouse project page: https://github.com/wheelhouse-project/WheelHouse -- open an issue or start a discussion there. Include what you tried, what you expected, and what happened instead; if the installer printed an error, paste it in full.

---

Generated: 2026-04-07 (regenerated 2026-07-17 from current sources for the v1.0.2 release, wh-help-doc-regen)
WheelHouse version: 1.0.3
