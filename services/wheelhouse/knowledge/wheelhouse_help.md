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
  provided to you -- this help document, the installation guide, and the separate Wheelhouse
  command and configuration reference when they are provided. Never invent features,
  commands, or settings that none of the provided documents describe. For the
  exact wording of a voice command, or a configuration setting and its default
  value, use the command and configuration reference when it is available: this
  help document explains the features in prose, but the complete list of every
  command and setting now lives in that reference.
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
- About 10 GB of free disk space. Most of that is the speech model; the Python environments the program and its speech engines run in are the smaller part.

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
6. Say **"undo"** -- the new line from step 5 is reversed.
7. Say **"select all"** -- the text is highlighted.

If steps 4 to 7 produce the results described, the installation is working.

Wheelhouse starts in click-to-talk (toggle) mode and starts listening, which is why steps 4 to 7 need no click. One click on the floating button or the tray icon switches listening off, and another switches it back on. To hold a button down while speaking instead, see [Interaction Modes](#interaction-modes) below.

If a step does not behave as described, see [Setup verification](#setup-verification) in Troubleshooting, or ask the Wheelhouse Assistant, which answers questions about any part of this document ([Getting Help](#getting-help)).

---

## Where to Go Next

Once the quick start works, continue with the section that matches the task.

- **Dictating text into email, documents and chat:** [Voice Commands](#voice-commands), in particular the dictation and punctuation subsections, then [Speech Modes](#speech-modes).
- **Using the full command set:** the complete [Voice Commands](#voice-commands) reference, covering commands, formatting and navigation, then [Configuration](#configuration). Every shipped command and every user-facing setting in config.toml is also listed in the [command and configuration reference](https://wheelhouse-project.org/reference.html).
- **Installing, configuring or diagnosing a fault:** [Installation and Setup](wheelhouse_install.md#installation-and-setup), then [Configuration](#configuration), then [Troubleshooting](#troubleshooting). For a question that none of those answer directly, the Wheelhouse Assistant answers from this document in plain language; see [Getting Help](#getting-help).

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
6. Open the settings file, `%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\config.toml`, in Notepad. Find the `[stt.google]` section and put the full path to your key file in `credentials_file`, between the quotation marks. Write each backslash twice, because TOML reads a single backslash as an escape character:

```
[stt.google]
credentials_file = "C:\\Users\\yourname\\keys\\wheelhouse-speech.json"
```

7. Save the file and restart Wheelhouse. The engine reads the key when it starts.

There is a second method, and it needs no file editing: set an environment variable named GOOGLE_APPLICATION_CREDENTIALS to the full path of the key file (press the Windows key, type "environment variables", open "Edit environment variables for your account"), then close Wheelhouse and start it again from the Start menu. The menu item "Restart Wheelhouse" is not enough for this method: part of the program keeps running across it, and that part still holds the environment from before you set the variable. Google's own software reads that variable automatically. Wheelhouse uses the variable whenever `credentials_file` is empty, so set one or the other, not both.

<!-- install-doc:end -->

<!-- install-doc:start -->

### Adding or switching engines later

To switch between engines already set up on this computer, right-click either the floating button or the tray icon -- both open the same menu -- open **STT Provider**, and select the engine. The change takes effect at once: Wheelhouse stops the running engine, starts the one you chose, and then records it as last_provider in the stt section of the settings file so the next start comes back on it. The choice is recorded as soon as the new engine's process starts. If that process cannot be started at all, the choice is not recorded and the next start returns to the previous engine. If the process starts and the engine then fails its own startup, the choice stays recorded and the next start tries that engine again; select a working engine from the menu to change it. Switching to Google Cloud this way does not set up its credentials; see the Google Cloud section above.

To add an engine that was never set up on this machine, re-run the installer and select that engine at its speech-engine question. The installer downloads and sets up what that engine requires, except that Distil-Whisper's model is downloaded by the engine itself the first time it starts. For example, moving from Google Cloud to Parakeet requires the re-run, because that is what downloads Parakeet's speech model; selecting it from the menu alone is not sufficient. Distil-Whisper is always added this way, since the installer sets it up only when it is selected.

The same re-run repairs a missing or incomplete speech model, for example after an interrupted download. The installer detects an incomplete model and reinstalls it. Re-running the installer is safe at any time, and the speech-engine question defaults to the engine already installed, so pressing Enter keeps it. If the current engine is no longer available on this hardware, the PowerShell installer reports that before asking; the setup wizard does not.

<!-- install-doc:end -->

### Teaching Wheelhouse your voice

The Distil-Whisper engine sometimes misses short words spoken alone, such as "comma". A short session teaches Wheelhouse how you sound so it stops missing them. Start it by saying "learn my voice" or "calibrate my voice", or right-click either the floating button or the tray icon -- both open the same menu -- open **STT Provider**, and choose **Teach WheelHouse your voice...**.

The session shows four words, one at a time, and asks you to say each word five times. It then asks for a cough or a throat clear, three separate times, to tell your words apart from your other sounds; this part can be skipped. No single step has a time limit, but if twenty minutes pass with no word or sound captured and no button selected, the session cancels itself, saving nothing. While the session is open, spoken words are used for the session only; nothing is typed into any window. The exception is an utterance that begins with the word "click", which passes to normal processing. Clicking by voice does not reach the window's buttons: "x-ray click cancel" begins with the hotword, so the session discards it, and "click cancel" without the hotword is not a click command, so it is dictated. Use the mouse or the keyboard for the window's buttons.

**Apply** saves what the session learned and restarts the listening, which takes about ten seconds. Only the Distil-Whisper engine uses this teaching; the other engines do not need it. If a different engine is active, the window says so and changes nothing.

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

Wheelhouse converts speech into keystrokes, text, and system actions. Most commands require no prefix. The hotword protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands require **"x-ray"** first; they are written here with the "x-ray" prefix.

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
| delete word (or erase word) | Deletes the whole word the cursor is on |
| submit | Presses Enter |
| go home | Jumps the cursor to the start of the line |
| go end | Jumps the cursor to the end of the line |

### Usage Examples

**Example 1 -- Dictating and correcting an email**

1. Dictate the body of the message, speaking the punctuation inline: "hi team comma new paragraph the release is ready period"
2. To correct a mistyped word, say **"backspace 2"** to remove the last two characters, then dictate the word again.
3. To correct the capitals, numbers, and punctuation of a paragraph, select it with **"select paragraph"**, then say **"x-ray fix"**, which sends it to the AI server and replaces it with the corrected version.
4. Say **"x-ray activate outlook"** (substitute your mail application) to bring its window forward, starting it if it is not already open, then **"submit"** to press Enter.

**Example 2 -- Searching for copied text**

1. Select a phrase with the mouse, or say "select word".
2. Say **"copy"**.
3. Say **"browser"**, as the whole utterance, to bring the default browser forward.
4. Say **"paste"** with the address bar focused, then **"submit"** to press Enter.

### Full Voice Command Reference

Every voice command and replacement is listed, one row each, in the [command and configuration reference](https://wheelhouse-project.org/reference.html). This section covers the behavior a table cannot express: how to dictate a word that is also a command, the key names accepted by "press", and how navigation, punctuation, and clicking behave.

#### Dictation Control

These commands control what is typed and let you dictate a word that collides with a command. "literal [words]" types the words that follow exactly, bypassing all command and replacement processing. "insert [text]" inserts raw text with no capitalization, spacing, or formatting. "submit" presses Enter, and is also recognized as the last word of a sentence: "hello world submit" types "hello world" and then presses Enter. To type the word itself, say "literal submit".

Utterances beginning with "okay Google", "ok Google", or "hey Google" are discarded, so speech aimed at a nearby voice assistant is not transcribed.

#### Text Editing

Common mishearings of "undo" and "redo" ("undue", "undu", "redu") are accepted, so the command still fires on those spellings. Deletion counts for "backspace" and "delete" are capped at 50. "tab [number]" requires the number. A bare "tab" spoken on its own presses Tab once; "tab" inside a longer sentence is typed as the word.

Say "erase" in place of "delete" in any delete command. "erase word", "erase all" and "erase 5" do the same as "delete word", "delete all" and "delete 5". The word "backspace" has no such alternative.

Wherever a command takes **[number]**, say the count either way: as digits ("backspace 15") or as words ("backspace fifteen", "delete twenty three"). Words up to "nine hundred ninety nine" are read, and each command still applies its own limit afterwards.

A spoken count can run to more than one word, so a command with a count waits until no further word could make the number larger, and at the latest until the end of the sentence. "backspace twenty three" deletes twenty-three characters, and "backspace 23" does the same. The wait depends on the number, not on how many words you used: "fifteen" and "ninety nine" are finished as spoken, while "twenty" and "fifty" could still grow into "twenty three" or "fifty five", so those wait for the next word. Every count command behaves this way, including "backspace". Chained "go" and "grab" moves, and moves without a unit word, read their counts differently, one word at a time; see the paragraph on counts further down this section.

##### The "press [keys]" Command in Detail

"press [keys]" sends any keyboard shortcut. Modifiers are held down first regardless of spoken order, so "press delete control" equals "press control delete". If any word in the phrase is unrecognized, nothing is pressed, and the whole phrase, "press" included, is typed as dictation. Hyphenated tokens from the speech engine, such as "f-11" or "control-alt", are split automatically.

**Modifier keys**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: f1 through f12.

**Letters**: any single letter a through z.

**Digits**: 0 through 9 are key names in any position, so "press control 2" presses Ctrl+2. "press" takes no repeat count.

**Symbols by spoken name**: the following are pressed correctly -- backtick, semicolon, slash (forward slash), backslash (back slash), comma, period (dot), single quote (apostrophe), left/right bracket (open/close bracket), equals (equal), minus (hyphen, dash), and the parentheses: left parenthesis (left paren, open parenthesis, open paren) presses Shift+9, and right parenthesis (right paren, close parenthesis, close paren) presses Shift+0, which type "(" and ")" on a US keyboard layout. Other symbol names are not reliable in "press": the shifted symbols (colon, tilde, pipe, question mark, double quote, braces, less than, greater than, plus, underscore) produce the wrong character, and hash, at, ampersand, asterisk, caret, percent, dollar, and exclamation press nothing. To type any of those, dictate them as punctuation words instead; [Punctuation and Symbols](#punctuation-and-symbols) below handles every symbol.

**Examples**: "press control shift t", "press f5", "press alt f4", "press windows d", "press left bracket".

#### Text Formatting

Formatting commands apply to the current selection: select first, with the mouse or "select word" / "select line", then say the command. The case and shape transforms cover UPPERCASE, lowercase, capitalize, title case, and the programming styles snake_case, camelCase, PascalCase, and kebab-case. The wrapping commands ("parentheses", "brackets", "braces", "angle brackets", "quotes", "single quotes") enclose the selection in those characters; spoken with no selection, they insert an empty pair with the cursor between them. Words spoken after a wrapping word in the same utterance are wrapped verbatim: symbol words such as "colon" are typed literally rather than converted. Three commands apply character formatting through the host application's own keyboard shortcuts: "bold text", "italics", and "underline". None of the three needs the hotword, and each fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed. Twenty-four more commands select a range and format it in one step: "bold", "italicize", or "underline", then "next" or "previous" ("last" also works), an optional count, and a unit -- words, lines, paragraphs, or characters. For example, "bold next 3 words" or "underline previous line". These also need no hotword and fire only as your whole utterance.

#### Navigation

"go" moves the cursor; "grab" moves it while extending the selection. Several moves chain in one utterance with "then". The utterance must begin with "go": "grab" is valid only chained after a "go" move -- "go home then grab to end". Spoken on its own, "grab ..." is typed as dictation.

Counts can be digits ("3") or spoken words ("one", "fifteen", "twenty three"). A single move that names a unit accepts a count of up to five words: "go right twenty three words", "go up 3 lines", "go left 5 characters", "go down 2 paragraphs". In a chained move, and in a move without a unit word, a count is one word, so say "twenty-three" there as a single hyphenated word rather than two separate words. Counts above 50 move 50. A digit count above 999 is typed as dictation instead of moving the cursor; leading zeroes do not count, so "0001" still moves one. "to", "too", and "for" are accepted as sound-alikes for 2 and 4, so "go right to words" moves two words. If any part of a "go" utterance cannot be parsed, the whole phrase is typed as dictation instead, so an unrecognized phrase does not move the cursor.

#### Punctuation and Symbols

Punctuation and symbol words are replacements: spoken as part of a sentence, the symbol is typed in place of the word, no pause required. Every punctuation and symbol word -- period, comma, colon, question mark, and the rest -- behaves this way.

Two mishearing tolerances are built in, because the default local engine frequently mishears "comma" and "colon": spoken as an entire utterance, **"colin"** inserts ":" and **"come"**, **"kama"**, **"commer"**, or **"come on"** inserts ",". Within a longer sentence these words are dictated normally. To type one as a standalone word, use "literal come" or "literal colin".

If the recognizer regularly mishears another word, add a personal correction in the Pattern Manager ("patterns"); it applies inline like the built-in punctuation words.

#### Application Switching and System

"x-ray activate [app name]" brings the named application's window forward. When nothing by that name has a window open, Wheelhouse starts the program instead of doing nothing: a pattern whose target is a program file (.exe) is run directly, and a spoken name is looked up among your installed programs, by Start menu shortcut and by the names Windows itself resolves. An exact name is preferred; failing that, a name that begins with the words you said. If more than one installed program matches, Wheelhouse starts none of them and lists the names, so you can say the full name of the one you meant; if none matches, it says so. The built-in "notepad" and "browser" take the program-file path, and "browser" resolves to the Windows default browser when spoken. The System commands operate on windows and on Windows itself. Seven need the hotword: "x-ray close window", because it discards unsaved work; "x-ray close tab"; the four searches, "x-ray search windows for [text]", "x-ray search on google for [text]", "x-ray search on bing for [text]", and "x-ray search on youtube for [text]"; and the short Google form "x-ray search for [text]", in which the word "for" is optional. Every other System command works without the hotword, and each of those fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed. Those include "zoom in", "zoom out", "create tab", "create window", "windows settings", which opens Windows Settings, "maximize", "minimize", and "desktop", which shows the desktop. In most browsers "create tab" (Ctrl+N) opens a new window rather than a tab, and "create window" (Ctrl+Shift+N) opens a private or incognito window.

#### Mouse Control

The **mouse grid** moves the pointer to any point on screen by voice, including points clicking by name cannot reach: applications that hide their controls from accessibility tools, drawing canvases, maps, games. Say **"show grid"** to lay a numbered three-by-three grid over the screen the front window is on. Saying a cell's number, **1 through 9**, redraws the grid inside that cell; repeat until the center sits on the target. The word "number" may precede each number -- engines hear "number five" more reliably than a single word. Then say the action:

| Say | What happens |
|---|---|
| click | left click at the center of the current cell |
| double click | double click there |
| right click | right click there |
| move here | moves the pointer there without clicking |
| mark | pins the start point of a drag and restarts the grid |
| drag | drags from the pinned point to the current cell center |
| hide grid | closes the grid without clicking |
| grid next screen | moves the grid to the next monitor |

A number in the same utterance narrows first: "x-ray click 5" narrows into cell 5, then clicks its center. All actions close the grid except "mark", "grid next screen", and a "drag" with no mark.

**Dragging** is two steps. Navigate to the point to drag from and say **"mark"** -- a pin appears and the grid restarts at full size. Navigate to the destination and say **"drag"**: the left button is pressed at the pin, the pointer moves gradually -- many applications ignore a pointer that jumps instantly -- and the button is released. No button is held while you navigate, so a pause or a misheard word between the steps cannot drop anything in the wrong place. "drag" without a "mark" shows a notice and presses nothing; the pin is forgotten when the grid closes.

While the grid is open, a spoken number on its own belongs to the grid and is not typed. With the grid closed and the numbered overlay showing, a number spoken as your whole utterance clicks the control with that label. With neither showing, the same words are ordinary dictation. The grid and the numbered overlay are never open together: "show grid" removes the numbers, "show numbers" closes the grid. Narrowing stops once cells reach `grid_min_cell_px` (click section of the settings file) -- a cell that small is already within a click's precision -- and drag speed is `drag_duration_ms` next to it.

No commands move the pointer continuously (no "mouse up" / "mouse down"); the grid places it at a chosen point instead. Volume and screen brightness are mapped to the thumb wheel of a Logitech MX-series mouse; see [Plugins](#plugins).

#### Voice Element Clicking

Wheelhouse can click buttons, links, menu items, and other on-screen controls. A control is selected in one of two ways: by its **name**, or by displaying a **number** on every clickable control and clicking that number -- "x-ray click 5". The numbered overlay covers controls with no spoken name, such as icon-only toolbar buttons, and cases where several controls share a name.

**Clicking by name**: say "click", then the name of the control. "the" may precede the name and is ignored; a role word may follow to restrict the search to one kind of control. Plain "click [name]" needs the "x-ray" hotword first -- say "x-ray click cancel" -- because otherwise any sentence opening with "click" or "tap" would be taken as a command. "right click [name]" and "double click [name]" need no hotword. **Role words**: **button**, **link** (a hyperlink), **menu** (a menu item), **tab**, **checkbox** (or **check box**), and **box** / **field** / **input** (a text entry field). With no role word, any clickable control matching the name is considered. A role word spoken with no name -- "x-ray click button" -- is treated as a name and searches for a control named "button".

**Right click and double click**: "right click [name]" and "double click [name]" work wherever "click [name]" works, and "right click 3" / "double click 3" work while the numbered overlay is showing. These press a real mouse click at the control's center, with the same safety checks as a normal click. They cover what a normal click cannot: a right click opens a control's context menu, and File Explorer items open on a double click.

**The numbered overlay**: "show numbers" displays a number on every clickable control in the front window, "x-ray click 3" clicks the control labelled 3, and "hide numbers" removes the numbers. Until then they stay up: clicking a numbered control repaints them in place, and they follow whichever window is in front. When "click [name]" matches more than one control closely, the numbers appear on those candidates only. With the mouse grid closed, a number spoken as your whole utterance ("7", "number seventy four") also clicks the control with that label; while the grid is open, a bare number belongs to the grid. A control whose own name is a digit -- a calculator "7" -- needs a role word: "x-ray click 7 button" clicks the calculator key rather than the control labelled 7. If the numbers no longer align after a page scrolls or changes, say "show numbers" again. Over the Pattern Manager the numbers follow its list: when you filter, add, remove, edit, expand, or collapse rows, Wheelhouse numbers the list again, and clicking the number of a pattern or category row selects that row and shows its details.

**Outcomes**: a successful click produces no notice. A failure produces a brief notice near the floating button and the tray icon: **not found** ("No match for [name]"; the numbered overlay is the alternative), **ambiguous** (the numbered overlay opens on the candidates; the "Found [A] and [B] -- be more specific" notice appears only when the overlay cannot open), and **could not complete the click** (the wording states the reason: control disabled, click timed out, or overlay stale and must be reapplied). Notices are rate-limited, so repeated failures do not produce repeated notices.

#### Wheelhouse Control

These commands control Wheelhouse itself: listening modes, help, personal patterns, and the AI features. "push to talk mode" and "click to talk mode" switch between the two listening modes. "x-ray fix" sends the selected text to the configured AI server for formatting correction -- capitals, numbers, amounts of money, common abbreviations, and obvious punctuation -- and replaces the selection with the result. The AI is told not to reword the text or add or remove words. The command requires that server to be configured and reachable, shows its progress on screen rather than out loud ("Correcting..."), then a notice with the outcome (for example "Done.", "No changes needed." or "Cancelled."), and leaves the original text in place if the request fails. Five further commands rewrite the selection instead: "simplify", "shorten", "x-ray make formal", "pirate", and "x-ray translate to [language]"; see "Rewriting the selected text" under [Selected Commands in Detail](#selected-commands-in-detail), which also covers adding others. "boost" adds the selected text to the speech recognition hints when the running engine applies hints (see "boost" below), "patterns" opens the Pattern Manager, "help" opens the Wheelhouse Assistant in the default browser after a short explanation window, and "x-ray cancel fix" stops a correction or rewrite still running. Every command in this paragraph written without the "x-ray" prefix needs no hotword, and each of those fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed.

Three further commands act on text through the host application: "x-ray find [text]" opens its find box and searches for the words spoken, "replace" opens its find-and-replace box, and "search" copies the selection and searches the web for it in the default browser.

"stop listening", said as the whole utterance, switches the microphone off. No voice command switches it on, because nothing is transcribed while listening is off. In toggle mode the wake word ("computer") is the voice route back on, and you can also click either the floating button or the tray icon. In push-to-talk mode the wake word ends only the idle pause, so after "stop listening" the way back is the next hold of the floating button; listening lasts only as long as that button is held, the hold works on the floating button alone, and a click on either surface has no effect. The right-click menu's **Speech Enabled** item is the exception: in push-to-talk mode it switches listening on and changes the mode to toggle mode.

The in-app help chat window is disabled in this release, and the voice patterns that opened it are switched off. "help" opens the Wheelhouse Assistant in the browser; see [Getting Help](#getting-help).

### Selected Commands in Detail

**"literal [words]"**

Say "literal" followed by the text to be typed, and those words are inserted without being matched against any command or replacement pattern. This is how to dictate a phrase that would otherwise trigger a command: "literal copy" types the word "copy" instead of copying, "literal period" types the word instead of a full stop, and "literal new line" types the phrase instead of a line break.

"literal" takes effect wherever it appears in an utterance: everything after it is typed exactly as spoken, and "literal" itself is not typed. To type the word "literal", say "literal literal".

**"boost"**

When the recognizer repeatedly mishears a specific word -- typically a name, a product, or a technical term -- select it, with the mouse or with "select word", and say **"boost"**, by itself. When the running engine applies hints, the selection is saved as a recognition hint in a shared hints file that persists across restarts, so each word needs boosting once. Hints are capped at 100 characters; boost words or short phrases, not sentences.

**Only an engine that applies hints accepts "boost".** **Google Cloud Speech-to-Text** applies saved hints without further configuration. **Parakeet, the default engine**, applies them only when hint biasing is on. Hint biasing is off by default because it slowed recognition by roughly 25 percent per utterance in the project's measurements. To switch it on, set enabled = true under [hotwords] in Parakeet's own config file. Then restart Wheelhouse, and accept the slower recognition. With biasing on, Parakeet accepts "boost" when its hints loaded, or when no hint is saved yet. If its hints failed to load, its ready notice says why. **Distil-Whisper** applies hints only when the [hotwords] switch in its config file is on. That switch is off as shipped, because biasing degrades its recognition, and it is marked for experiments only.

**Under an engine that does not apply hints, "boost" is dictation.** Wheelhouse types the word "boost" in place of the selection. It copies nothing, saves no hint, and shows no notice. In most applications, "undo" removes the typed word and restores the selected text. The same rule covers any pattern with the action "Teach word to speech engine", including a pattern you made yourself. The Pattern Manager's "Try it" field gives the same answer: under such an engine it reports no command for "boost".

**"patterns" (the Pattern Manager)**

This opens the **Pattern Manager**, which lists every voice command and text replacement, grouped by category. Selecting an entry shows its trigger phrase, its action, and whether it requires the hotword.

The Pattern Manager can **view** any pattern, including every built-in one; **create** personal patterns, such as a shortcut that types an email address, a correction for a misheard word, or a command that opens a program; **edit** and **delete** user-created patterns; **customize** a built-in pattern, where a personal copy with the same trigger overrides it and deleting that copy restores the shipped behavior; and **change the command hotword** from "x-ray" to another word.

Personal patterns live in a separate per-machine file, preserved across upgrades; the shipped patterns file is never modified. That file is `user_patterns.toml`, in the folder `%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\data`. It does not exist until you first save a pattern or change the hotword, and the Pattern Manager is the tool that edits it.

**Running a program from a pattern.** Two actions run programs. **Run a program** starts a program or a command line and does not wait for it to finish. **Run a program and capture its text** runs one program without a shell, waits for it to finish, and keeps the text it printed for the steps after it. A Python script runs the same way, through the Python interpreter. For example, a pattern that inserts what `C:\scripts\example.py` prints needs two steps in advanced mode. The first step uses **Run a program and capture its text**, with `python` in its program field and `C:\scripts\example.py` in an argument field that **Add argument** creates. The second step uses **Insert text**, with `run_capture` as its text: that is the name of the first step's action, and it stands for the text the script printed, which the step inserts at the cursor. The capture waits for the number of seconds in its timeout field, or for 10 seconds when that field is empty, and never for longer than 60 seconds. The 10-second default is the setting run_capture_timeout_default_s under [actions]. The printed text can hold at most output_cap_chars characters, a setting under [actions] that is 10000 by default. Longer output is not cut short: the step fails instead. The step also fails when the program is still running at the end of the wait, which stops the program, and when the program exits with an error code. A failed step stops the pattern, so nothing is inserted, and Wheelhouse types the spoken words as ordinary dictation instead. Every action and its parameters are listed in the [Action Reference](https://wheelhouse-project.org/reference.html#action-reference).

**Rewriting the selected text**

With text selected, **"simplify"** sends it to the AI server to be rewritten in plain language and replaces the selection with the result. **"shorten"** removes repetition and padding, **"x-ray make formal"** removes contractions and casual wording, and **"pirate"** rewrites the text in pirate speech. **"x-ray translate to [language]"** translates the selection into the language named -- "x-ray translate to spanish", "x-ray translate to brazilian portuguese"; multi-word names are recognised. All five use the same AI server as "x-ray fix" and leave the original in place if the request fails; they show "Rewriting..." where "x-ray fix" shows "Correcting...", then an outcome notice.

A rewrite pastes back plain text, so word-processor character formatting -- bold, italics, a font, a colour -- is lost on the rewritten part. Layout is preserved: line breaks, blank lines, indentation, bullets, numbering, and non-sentence lines such as a code line or a postal address return unchanged.

**Adding a rewrite command.** The five commands are one action carrying a different instruction each, so more can be added from the Pattern Manager without changing program code: create a pattern, choose the **Rewrite text with AI** action, and set its parameter to a sentence telling the AI the style required.

For example, to rewrite text at a reading level a younger reader can follow, create a pattern with the trigger **reading level** and this instruction:

> Rewrite this text so a ten-year-old could read it. Use short sentences and everyday words, and explain any term a ten-year-old would not know. Keep every fact. Return only the rewritten text.

Selecting a paragraph and saying "x-ray reading level" then rewrites it. Two constraints on that instruction: **describe the style only** -- Wheelhouse itself supplies the wording that preserves layout and keeps instructions inside the selected text from redirecting the AI, and duplicating either degrades the result -- and **end with "Return only the rewritten text."**, without which the AI is liable to prefix a sentence of commentary that is then pasted into the document.

**"x-ray cancel fix"** stops a request that is still running -- a correction or any of the rewrites -- and nothing is pasted.

**"help"**

Opens the Wheelhouse Assistant, the project's online help, in the default browser, where questions can be asked in plain language. The address is the gem_url setting in [ai.help], which points at the assistant by default. If the setting still holds the address of the retired ChatGPT assistant, which earlier releases shipped as the default, "help" opens the Wheelhouse Assistant instead.

A short explanation window comes first, the same one the **Help** menu item shows: it explains that the assistant runs inside Google Gemini and that a sign-in is needed, and its **Assistant** button opens the browser. Ticking **Do not show this again** and selecting **Assistant** sets explain_before_open to false under [ai.help], after which "help" opens the browser directly. See [Getting Help](#getting-help).

## Speech Modes

Wheelhouse has no command mode and no dictation mode to switch between. Each utterance is classified as it arrives, using the position of the words within the phrase.

### Classification of spoken input

- **Command**: the utterance matches a voice command and the command is performed. "undo" presses the undo shortcut; "delete five" deletes five characters. "erase five" does the same, because "erase" is a synonym for "delete". Nothing is typed.
- **Dictation**: the utterance is typed into the focused text field. "dear Sarah thank you for the update" is typed as those words.
- **Inline replacement**: certain words are replaced with symbols or corrected spellings within dictation. "hello comma world" produces "hello, world"; the word "comma" is replaced by the punctuation mark rather than typed.

### Word position determines classification

- **The first word of a phrase is a candidate command.** When speech starts after a pause, the first word is checked against the set of known command openings. If it could begin a command, it is held briefly -- well under a second -- to see whether the following word or two completes one. "delete five" spoken as its own phrase runs the command. If the words match no command, they are typed as ordinary text; no words are discarded.
- **Words in the middle of a phrase are dictation.** "I want to delete five items" is typed in full, including the word "delete", because "delete" did not begin the phrase.
- **Replacement words apply in any position.** Words such as "comma" and "period" are replaced whether they occur first, last, or mid-sentence, since their purpose is to appear within dictation.

### Hotword-protected commands

The hotword protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands run only when the utterance begins with "x-ray", as in "x-ray close window". Other commands do not require it. The hotword follows the same position rule: "x-ray" carries its special meaning only as the first word of a phrase, and is typed as text anywhere else. If "x-ray" is followed by something that is not a command, the whole phrase including "x-ray" is typed as text.

### Streaming insertion

Recognized words are inserted while speech continues, rather than after the utterance ends. Two exceptions are brief holds, both fractions of a second: one at the start of a phrase while a command match is evaluated, and a similar one around replacement words. A third exception is not a hold. While Wheelhouse reads the window -- to put numbers on the controls, or to find a control named in a "click" command -- a word spoken in that moment is dropped instead of typed, and it is not typed afterwards. Wheelhouse drops it deliberately: a word held until the read finished would be typed seconds later, into whatever had focus by then. The refusal lasts no longer than the read itself, and it cannot continue without limit.

### Chaining cursor moves with "then"

Cursor movements and text selections can be chained into one phrase with "then":

- "go home then grab to end" -- moves to the start of the line, then selects to the end of the line.
- "go top then grab to bottom" -- moves to the top of the document, then selects to the bottom.

Chaining applies only to the "go" (move the cursor) and "grab" (extend the selection) navigation commands. All other commands, including copy, paste, and window switching, are spoken as separate phrases.

## Interaction Modes

Speech modes, described above, govern how recognized words are classified. Interaction modes govern when Wheelhouse listens. There are two, and they can be switched at any time.

### Toggle mode

Wheelhouse listens continuously while speech is switched on. One click on the floating button, or one left-click on the tray icon, switches listening off; another click switches it back on. This is the mode for hands-free operation: once listening is on, no further input device is required.

In toggle mode, pressing and holding the floating button for about a fifth of a second or longer listens for the duration of the hold. On release, listening returns to the state it had before the hold: off if it was off, and still on if it was on. This is useful when listening is normally off and a single command is to be spoken.

### Push-to-talk mode

Wheelhouse listens only while the floating button is held down. Press and hold to speak; releasing stops listening. During the hold, the computer's speakers are muted, so that sound from a video or from music cannot reach the microphone and be transcribed; the previous volume is restored on release. The System Volume plugin performs this muting ([Plugins](#plugins)); it is enabled by default, and disabling it also disables the muting. In this mode a single left-click on the tray icon has no effect; the hold operates on the floating button.

Three constraints apply:

- **Audio already playing.** When the sound pause applies (see `ENABLE_AUDIO_SUPPRESSION` in [Plugins](#plugins)), a hold can still listen over the sound, but only when the speakers are really muted: the hold mutes them and ignores the pause at once, and it goes on ignoring the pause only if the System Volume plugin confirms the mute. If no confirmation arrives -- the plugin is disabled, or no audio device answers -- the pause applies to the hold about half a second after it starts, and the button shows that listening is off. In that case, pause the audio first; listening resumes once the sound-level check notices the silence, which can take up to about thirteen seconds after the audio stops. A Sonos speaker playing music still pauses a hold, because muting this computer cannot silence it.
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
- **Amber with "..."**: you are holding the button, and Wheelhouse has asked to start listening but has no answer yet. It turns purple if no answer arrives.
- **Purple with "!"**: the hold did not start listening, or listening stopped. The reason is in the button's tooltip, and Wheelhouse shows it as a notice as well.
- **Solid red**: listening is on.
- **Pulsing red to orange**: speech is being received.
- **A brief flash of green**: the last utterance has been processed.
- **A small white dot in the lower-right corner**: a requested control is being searched for in the window. The dot is drawn over whichever colour is currently showing.

Available actions:

- **Click** to switch listening off, and click again to switch it on. In push-to-talk mode a click has no effect; that mode listens only during a hold.
- **Press and hold** for about a fifth of a second or longer to listen for the duration of the hold. On release, listening returns to the state it had before the hold, so in toggle mode with listening already on, it stays on. This works in either interaction mode.
- **Double-click** to switch between toggle mode and push-to-talk mode.
- **Drag from the middle** to move the button. Its position is saved and restored at the next start.
- **Drag the outer edge** to resize it. It resizes around its own centre, so the point being dragged keeps its position relative to the middle. Both the size and the resulting position are saved.
- **Hold Ctrl and roll the mouse wheel** over it to resize it. Both gestures use the same limits.
- **Right-click** to open the menu described below.

The resize area is a ring 8 pixels wide around the outer edge. The area inside that ring moves the button and starts push-to-talk. The pointer changes to a diagonal arrow over the ring. On a small button the ring narrows to a third of the radius, so that a movable centre always remains. The button is constrained to between 15 and 150 pixels across. If a resize would place the button off the edge of the screen, it is moved back into view. At start-up, after a drag, and after a monitor is added or disconnected or the display resolution changes, Wheelhouse checks that at least half of the button is on a screen. If less than half is, it moves the whole button onto the nearest screen and saves that position. A button parked over the taskbar, or partly past an edge with half of it still showing, stays where it is.

To hide the button, right-click it and switch off "Show Floating Button". The same menu item on the tray icon restores it.

### The tray icon

The tray icon is the Wheelhouse logo in the notification area near the clock. Its appearance is fixed and does not change with the state of the program. The floating button reports state; the tray icon keeps Wheelhouse reachable when the floating button is hidden.

- **Left-click** to switch listening off and on, as with the floating button. In push-to-talk mode a left-click has no effect; the hold gesture works only on the floating button.
- **Double-click** to switch between toggle mode and push-to-talk mode.
- **Right-click** to open the menu below.

If the icon is absent, Wheelhouse did not finish starting; see [Troubleshooting](#troubleshooting).

### The right-click menu

The floating button and the tray icon open the same menu. Most items are unavailable until Wheelhouse has finished starting. "About Wheelhouse" is always available, because it depends on no other part of the program.

- **Speech Enabled** -- switch listening on or off. The checkmark shows the current state. In push-to-talk mode, selecting it switches listening on and changes the mode to toggle mode.
- **Show Floating Button** -- show or hide the floating button. The checkmark shows whether it is visible.
- **Interim Results** -- selects whether words are typed as they are recognized and corrected afterwards, or held until the phrase ends. The first is the streaming insertion described under [Speech Modes](#speech-modes); switching this off trades the immediate feedback for text that arrives already settled.
- **Push-to-Talk Mode** -- switch between the two interaction modes. The checkmark shows when push-to-talk is active.
- **STT Provider** -- select the speech engine. Only engines set up on this computer are listed. See [Speech Engines](#speech-engines). The last item in this list, **Teach WheelHouse your voice...**, opens the voice-teaching session for the Distil-Whisper engine. See [Teaching Wheelhouse your voice](#teaching-wheelhouse-your-voice).
- **AI Model** -- select the AI model. The list holds the model named in the settings file and, for a local AI server, the models that server offers. The configured model always appears as a normal checked item, even when the server no longer offers it; the menu does not report a missing model. When the AI features are switched off or no AI server is configured, the list holds one unavailable item, "AI disabled" or "AI not configured".
- **Pattern Manager** -- open the editor for personal voice patterns. See [Voice Commands](#voice-commands).
- **Debug** -- switch detailed logging on or off. Leave it off except when diagnosing or reporting a problem.
- **Help** -- open the Wheelhouse Assistant in the browser. A short explanation window appears first, with an **Assistant** button that opens it and a **Cancel** button that does not; tick **Do not show this again** and then select **Assistant** to skip the window from then on. This is the same page, and the same window, that the spoken command "help" produces. See [Getting Help](#getting-help).
- **About Wheelhouse** -- show the program name and the running version. Include the version in any problem report.
- **Restart Wheelhouse** -- stop the speech engine and the Wheelhouse processes, then start them again. The launcher process that started Wheelhouse keeps running and starts the new processes. This is the first step when speech recognition stops responding.
- **Exit** -- close Wheelhouse. Required before running the installer to update, and before uninstalling.

## Configuration

No settings need to be edited to use Wheelhouse. Every value ships with a working default, and the most common choices -- which speech engine to use, push-to-talk versus click-to-talk -- can be changed from the floating button's or the tray icon's right-click menu without opening a file. The listening mode can also be changed by voice; the speech engine cannot. This section covers the adjustments available in the settings file.

**Where the settings file is.** Wheelhouse keeps its settings in a plain text file named config.toml, which opens in Notepad. It is at:

```
%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\config.toml
```

To open the folder, press the Windows key and R together, paste that folder path, and press Enter (`%LOCALAPPDATA%` expands to a folder Windows hides by default, so paste rather than browse). The installer creates config.toml from the template `config.toml.example` beside it. Your copy is personal to your machine and is never transmitted. Lines starting with a number sign are comments, and the file documents many of its own settings inline. Wheelhouse removes those comments the first time it saves a setting itself -- for example after you move or resize the floating button, or switch the listening mode or the speech engine. The template config.toml.example keeps them.

A few practical notes:

- Change one setting at a time, then restart Wheelhouse so the change takes effect.
- To restore the defaults, copy `config.toml.example` over `config.toml` in that same folder.
- The Sonos and Sony Bravia plugin sections near the bottom of the file are off by default; their comments say to turn them on only if you own that hardware.

**The per-setting reference** -- every user-facing key that the shipped config.toml contains, its default, and what it does -- is in the [command and configuration reference](https://wheelhouse-project.org/reference.html). The three optional Sonos settings that the file leaves commented out are described under [Plugins](#plugins). Two settings are worth knowing before opening it. Transcript logging (LOG_TRANSCRIPTS) is off by default, which keeps dictated words and clipboard contents out of the log files; turn it on only while diagnosing a recognition problem, then turn it back off. The AI server's API key is never stored in config.toml: if your server requires one, set the WHEELHOUSE_AI_API_KEY environment variable instead, keeping the key out of a file that could be copied or shared.

The rest of this section covers the two most common adjustments: performance on slower hardware, and recognition quality. For a setting not covered here, the Wheelhouse Assistant answers questions about any key in the reference; see [Getting Help](#getting-help).

### Settings for slower hardware

If Wheelhouse feels laggy or unreliable on an older computer, these changes help, roughly in order of impact:

1. **Pick the right speech engine.** The default "parakeet_tdt" ([stt] last_provider) runs on any CPU; do not switch to "distil_medium_en" without a capable recent graphics card. "google_stt" moves the work to the cloud -- at the cost of an account and an internet connection.
2. **Give yourself more speaking time.** Raise REPLACEMENT_TIMEOUT_MS and COMMAND_TIMEOUT_MS from 700 to 900-1000.
3. **Slow down text insertion.** Under [ui_actions.timing], raise post_paste_delay_ms (30 to 60), clipboard_operation_delay_ms (50 to 100), and clipboard_verification_timeout_ms (250 to 500) if dictated text arrives incomplete or garbled.
4. **Give voice clicking more time.** Under [click], raise response_timeout_ms (3000 to 5000) and walk_deadline_ms (2500 to 4000) if clicks time out in complex windows. Keep walk_deadline_ms more than 250 below response_timeout_ms, so raise response_timeout_ms first: otherwise Wheelhouse switches voice clicking off and the log names the setting. Raise screen_read_timeout_ms (10000 to 15000) if the numbered overlay says it could not draw the numbers in windows that take several seconds to answer.
5. **Allow a local AI server longer to answer.** Raise [ai.server] timeout_s from 30 to 60 if corrections time out -- or leave AI off; nothing else depends on it.

### Speech recognition quality settings

**The hallucination filter (Distil-Whisper engine only).** Whisper-family engines have a well-known quirk: fed a cough or background noise, they sometimes invent polite filler -- a stray "thank you" you never said. The Distil-Whisper engine ships with a confidence filter that discards such low-confidence utterances. Its threshold is **hallucination_logprob_threshold** (default -0.6) in the Distil-Whisper provider's own config file, not the main config.toml. The default was calibrated on one male voice with a studio microphone and may be too strict: if real speech is sometimes silently ignored -- more likely with a strong accent, quiet speech, or a laptop microphone -- lower it to -0.7 or -0.8. More negative is more permissive; a very large negative number turns the filter off. If no threshold works, switch to the Google engine from the floating button or tray icon menu: less affected by noise and voice variation, but a cloud service needing an account, and audio goes to Google. The filter does not apply to the default Parakeet engine, which neither produces the confidence signal nor shares the quirk to the same degree.

## Plugins

Plugins are optional add-ons that connect Wheelhouse to extra hardware and services: your laptop screen, Sonos speakers, a Sony TV, and a few Windows features. Each plugin described below has its own `[plugins.*]` section in config.toml with an `enabled` switch, so you can turn each one on or off without deleting anything. You do not need any of them for dictation and voice commands to work. A plugin that cannot find its hardware at startup turns itself off for the session and reports the reason in the log; the rest of Wheelhouse runs unaffected. Hardware that is present but unreachable is a different case: the Sonos plugin keeps polling a speaker whose address it resolved at startup, so a speaker that comes back on the network is picked up again without a restart.

Four of the plugins described below respond to the mouse thumb wheel -- the small horizontal wheel on the side of the mouse, under your thumb. This is not the main scroll wheel: that one keeps its normal scrolling job. Wheelhouse reads the thumb wheel directly from the device, which currently works only with Logitech MX-series mice. Screen zones pick what the thumb wheel controls: pointer at the left edge of the screen, it adjusts brightness; anywhere else, volume -- no command or click needed. Step size and zone width are adjustable in the configuration reference.

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

A second, separate mechanism can pause listening for computer audio, whether or not you own a Sonos. It is set by `ENABLE_AUDIO_SUPPRESSION`, which takes three values. With `"auto"`, the default, Wheelhouse asks Windows once at startup whether the default microphone has an echo canceller. When Windows reports one, sound this computer plays does not pause listening. When Windows reports none, or cannot answer, Wheelhouse watches the sound level of the Windows output device and pauses recognition while sound is playing through it. On a machine where Windows reports no echo canceller, sound from a screen reader also pauses listening. `true` pauses listening whenever sound plays, whatever Windows reports, and `false` never pauses. A settings file from an older install has `true`; change it to `"auto"` to let Windows decide. A microphone plugged in or made the default after startup is checked at the next start. While the pause applies, the check runs on an interval that widens to ten seconds while audio is playing, and listening resumes by itself only after the sound has stayed quiet for about three seconds, so listening can take up to about thirteen seconds to resume after the audio stops. In click-to-talk mode, saying the wake word ("computer") ends the pause at once, while the sound still plays. So audio your computer plays can pause listening -- including audio it plays through a Sonos, when the Sonos is the Windows output device. What the Windows output device never sees, and therefore never pauses listening for, is audio that reaches the Sonos without passing through this computer: television audio over HDMI or an optical cable, and music the Sonos streams by itself.

### System Volume

Controls the normal Windows volume (the same one as the taskbar speaker icon) from the volume scroll zone, and quiets system audio while you hold the push-to-talk button. Enable with `plugins.system_volume.enabled` (default: enabled). Settings:

- `device_type` -- which audio device to control: `"default"` (the usual choice) or `"communications"`.
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

Notices when you have stepped away (no keyboard or mouse activity) and pauses listening so Wheelhouse is not transcribing an empty room; listening resumes when you return or say the wake word. In push-to-talk mode the wake word ends only the idle pause, and listening then waits for your next hold of the floating button. Enable with `plugins.idle_monitor.enabled` (default: enabled). Settings: `idle_timeout_minutes` (default 10) and `polling_interval_seconds` (default 4). Fully local -- it only asks Windows how long since your last keypress or mouse move. Note that the measure is keyboard and mouse activity, not sound: watching a film without touching either pauses listening once the timeout passes.

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

Start with the verification checks below. The Wheelhouse Assistant can also read an error message or a log excerpt and identify the cause; see [Getting Help](#getting-help).

### Setup verification

Run these five checks in order. Stop at the first that fails and read the entry it names.

1. **Did the installer finish without error lines?** If not, see "Installer failures" below.
2. **Do Windows Sound settings show the microphone receiving sound?** Right-click the speaker icon on the taskbar, open Sound settings, open Input, and speak. If the input meter does not move, see "Microphone not detected."
3. **Is the Wheelhouse icon present in the system tray?** The tray icon is the check that matters: the floating button can be switched off from the menu on either surface, so its absence does not mean the program failed to start. If the tray icon is missing, see "Wheelhouse does not start and neither the tray icon nor the floating button appears."
4. **Open Notepad, click in the empty page, and say "hello".** If the word does not appear, see "Dictation not appearing in text fields."
5. **Say "undo".** If the word does not disappear, see "Commands not recognized."

If all five pass, the installation is working; any remaining problem is specific to one application or feature. The entries below cover the common cases.

### Common Problems

**Microphone not detected**

- *Symptom:* Wheelhouse starts, nothing happens when you speak, and Windows Sound settings show no input activity.
- *Likely cause:* Windows is using a different microphone, or a privacy setting is blocking desktop applications from the microphone.
- *Action:* Open Settings > Privacy and security > Microphone and confirm that "Let desktop apps access your microphone" is on. Then open Sound settings > Input and select the microphone in use. Restart Wheelhouse afterward.

**Wheelhouse does not start and neither the tray icon nor the floating button appears**

- *Symptom:* Wheelhouse is started and nothing appears. Judge by the tray icon; the floating button is hidden whenever "Show Floating Button" is off.
- *Likely cause:* One of the background processes failed during startup, most often because a speech model is missing or an earlier install was interrupted.
- *Action:* Re-run the installer. It repairs a broken install and preserves settings and personal data. If it still does not start, restart the computer and try once more before filing a report.

**Speech engine not connecting**

- *Symptom:* The speech engine is reported as disconnected, or Wheelhouse remains in a waiting state without recognizing speech.
- *Likely cause:* The speech engine failed to start. Common reasons: its model was never downloaded, the Google Cloud engine has no credentials, or the computer is low on memory.
- *Action:* Switch engines from the menu on the floating button or the tray icon; Parakeet, the built-in offline engine, needs no account. If the required engine was never set up, re-run the installer and select it at the engine question. For Google Cloud, set the key file's path in the settings file, or set the GOOGLE_APPLICATION_CREDENTIALS environment variable; the variable is consulted only when the settings file names no key. See [Speech Engines](#speech-engines). If the engine will not start right after an install or update, run "uv --version" in a new PowerShell window; uv not found means the installer's tooling is not on the PATH; re-running the installer corrects that.

**Speech will not start and a notice names the Microsoft Visual C++ runtime**

- *Symptom:* Wheelhouse starts, but speech never becomes ready, and a notice says speech needs a newer Microsoft Visual C++ runtime.
- *Likely cause:* The speech engine's native files need the Microsoft Visual C++ runtime. That runtime is not part of Windows, so a computer that never installed it, or installed an old copy, cannot load them. The speech process then stops within a few seconds of starting, and no text ever appears.
- *Action:* Run the Wheelhouse installer again and select Yes when Windows asks for permission. The installer then installs the runtime for you, and nothing else about your installation changes. If you already did that, restart the computer: Microsoft's installer sometimes needs a restart before Windows uses the new runtime. If you cannot use that route -- for example your account cannot approve the Windows permission prompt -- ask someone with an administrator account to install the Microsoft Visual C++ Redistributable (x64) from Microsoft's own page at https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist, under the heading "Visual C++ v14 Redistributable". Start Wheelhouse again afterward.

**Commands not recognized**

- *Symptom:* A command such as "maximize" has no effect, or is typed as text.
- *Likely cause:* The speech engine returned a different word, for example "maximum" instead of "maximize", or the utterance overlapped with playing audio.
- *Action:* Speak the command as a separate utterance, with a brief pause before it, at normal conversational volume; raised volume reduces accuracy. If one word is misheard repeatedly, select a correctly spelled copy and say "boost" -- see its entry in [Voice Commands](#voice-commands). Only Google applies hints as shipped; Parakeet and Distil-Whisper apply them only with hint biasing on. Otherwise "boost" is typed as a word.

**Command words are typed as text instead of running**

- *Symptom:* "close window" is typed into the document instead of closing the window.
- *Likely cause:* Expected behavior. The hotword protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands require "x-ray" first.
- *Action:* Say "x-ray close window". The command reference marks which commands require the hotword.

**Dictation not appearing in text fields**

- *Symptom:* Speech is recognized but no text appears in the application.
- *Likely cause:* The text field is not focused, or Wheelhouse could not confirm that the focused control accepts text. Wheelhouse then pastes your words with Ctrl+V instead of typing them, so the words arrive only where a paste works. A web browser page that is not a text field takes no paste, so words spoken while the page itself is focused arrive nowhere.
- *Action:* Click inside the text field and try again. If the application runs as administrator, Wheelhouse cannot reach it at all and shows a notice that says so; use the physical keyboard there, or run Wheelhouse as administrator as well. If dictation works in Notepad, the problem is specific to that application's text field.

**Real speech ignored, or short dictations dropped**

- *Symptom:* With the Distil-Whisper (graphics card) engine, short phrases -- or for some voices a substantial part of normal speech -- produce no text and no error.
- *Likely cause:* That engine's confidence filter, calibrated on one voice with a studio microphone, classifies real speech as noise and discards it. This is more likely with a strong accent, quiet speech, or a laptop's built-in microphone.
- *Action:* If the missed words are short ones spoken alone, such as "comma", run the voice-teaching session first: see [Teaching Wheelhouse your voice](#teaching-wheelhouse-your-voice). If longer speech is still ignored, lower hallucination_logprob_threshold in that engine's own config file from -0.6 to -0.7 or -0.8 (more negative is more permissive) and restart Wheelhouse. If no threshold works, switch engines from the menu on the floating button or the tray icon; the Google Cloud engine does not use this filter.

**Words from a playing video are typed, or listening pauses whenever sound plays**

- *Symptom:* Wheelhouse types the words of a video or other audio that is playing, or listening pauses every time the computer plays sound.
- *Likely cause:* The microphone's "Audio enhancements" setting in Windows is Off. With that value, Windows offers no echo canceller for the microphone, so the microphone also hears what the speakers play.
- *Action:* Open Settings > System > Sound > Input, select the microphone, and set "Audio enhancements" to "Device Default Effects" or "Voice Clarity". Either value gives the microphone the Windows echo canceller again. Then restart Wheelhouse: Wheelhouse asks Windows about the echo canceller only once, at start.
- *Check:* After the restart, the log file wheelhouse.log contains the line "Audio suppression off: Windows reports an echo canceller", followed by the microphone's name.

**AI text correction does nothing or times out**

- *Symptom:* Selected text is not corrected although AI is enabled. Text correction and rewriting are the built-in AI commands. A personal pattern can also send its own prompt to the AI with the **Ask AI** action, listed under Advanced actions in the Pattern Manager. The in-app help chat is currently disabled.
- *Likely cause:* Wheelhouse sends requests to an AI server rather than running the model inside itself -- either one you point it at, or one it starts on this machine when [ai.runtime] enabled is true and the command-line installer was run with `-AiMode local`. If that server is absent or unreachable -- Wheelhouse checks by asking it for its model list -- the AI commands show "AI is not available right now." and change nothing, and the rest of the program continues to run. A server that answers but lacks the configured model does not switch AI off: each request shows "The AI server doesn't have the configured model. Check the model name in the AI settings. Original text preserved." A server too slow to answer within the time limit makes the request fail with "Correction failed. Original text preserved.", or with "The AI server isn't responding. Original text preserved." when the server has also stopped answering the check.
- *Action, in order:* confirm [ai] enabled = true and [ai.server] base_url is set (an empty base_url disables AI by design); confirm the server is reachable at that address and that the [ai.server] model name is one it provides; raise [ai.server] timeout_s if the server is slow to respond; and for a remote server requiring a key, set the WHEELHOUSE_AI_API_KEY environment variable -- the key is never stored in the settings file -- and restart Wheelhouse.
- *Note:* An unreachable AI server does not affect dictation, voice commands, or any other feature.

<!-- install-doc:start -->

### Installer troubleshooting

**Installer failures**

Each installer failure message and its action is listed under [Installation failure messages](wheelhouse_install.md#installation-failure-messages); re-running the installer is safe and interrupted downloads resume.

<!-- install-doc:end -->

---

## Getting Help

**The Wheelhouse Assistant is the first place to ask.** It is a Google Gemini assistant holding this entire document, the full command and configuration reference, and the project's own notes on how each part behaves. It answers questions in plain language, reads an error message or a log excerpt and identifies the likely cause, and names the specific setting to change and the file it belongs in. It covers material this document summarizes, so it can answer questions no section here addresses.

<https://gemini.google.com/gem/1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A>

Three ways to reach it: the address above, the **Help** item in the right-click menu on the floating button or the tray icon, and the spoken command "help". All three open the same assistant in the default browser. Gemini asks for a sign-in first; a Google account, an Apple account, or an email address works, and the free tier is sufficient.

The menu item and the spoken command show a short explanation window first, headed **Ask the Wheelhouse Assistant**. It says what the assistant is, that it runs inside Google Gemini, that a sign-in is needed, and what signing in involves. Its **Assistant** button opens the assistant; **Cancel**, or the Escape key, closes the window and opens nothing. Tick **Do not show this again** and then select **Assistant**, and Help opens the browser directly from then on. If Windows cannot start a browser, Wheelhouse shows the notice "Wheelhouse could not open your browser." To bring the window back, set `explain_before_open = true` under `[ai.help]` in the settings file; see [Configuration](#configuration).

The assistant does not have access to a particular computer, so it cannot read logs that are not pasted into it, and it does not know about changes made after the release it was built from.

**Reporting a defect.** A problem in Wheelhouse itself -- something that behaves incorrectly rather than something that needs explaining -- goes to the project:

- Open an issue or start a discussion at https://github.com/wheelhouse-project/Wheelhouse.
- Or email `help@wheelhouse-project.org`.

Include the Wheelhouse version from **About Wheelhouse** in the right-click menu, what was done, what was expected, and what happened instead. Paste any error message in full, and attach the installer's setup log if the installer failed. Installer messages and log lines contain no dictated text unless transcript logging was switched on; see [Configuration](#configuration).

---

Generated: 2026-09-23 for the v1.2.0 release
Wheelhouse version: 1.2.0
