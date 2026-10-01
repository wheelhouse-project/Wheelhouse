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

## Overview

Wheelhouse controls a Windows PC by voice. It performs five functions: dictating text into any application, executing spoken commands, switching windows, launching programs, and clicking on-screen controls by name. None of them require the keyboard or mouse. On a Logitech MX-series mouse, Wheelhouse can additionally assign screen brightness and volume to the thumb wheel.

**Intended use.** Wheelhouse is a general-purpose voice interface and an assistive technology. A user for whom a keyboard and mouse are painful, difficult or impossible can operate the computer through the voice interface alone.

**Requirements.**

- A Windows 10 or Windows 11 PC (64-bit)
- A microphone. A laptop's built-in microphone is usually adequate. If recognition accuracy is poor, a headset or external microphone is worth trying, and one can be connected after installing.
- About 10 GB of free disk space. The speech model is 2.5 GB of that; the program and the Python environments its speech engines run in take the rest.

The installer provides everything else and checks the hardware before it begins. No account, subscription or prior installation of other software is required. The full requirements, including memory and processor, are in [System Requirements](#system-requirements) below.

**Where speech is processed.** By default, Wheelhouse converts speech to text on the local machine. No audio or text is transmitted, and the application reports no telemetry. Cloud speech recognition is available; it is not the default, and it can be selected at install time or afterwards. [Speech Engines](#speech-engines) compares the engines and states what each one transmits.

**Operation.** Wheelhouse converts microphone audio to text on the local machine and then classifies the result. Text matching a known command ("undo", "select all") is executed as that command. All other text is dictation, and is inserted into the focused window -- a document, an email, a chat field -- with capitalization and spacing applied automatically. Punctuation is spoken: "comma" and "period" insert the corresponding symbols. Text is inserted continuously while you speak, typically beginning within two seconds, rather than after the utterance ends. [Speech Modes](#speech-modes) documents how the classification is decided.

**Getting answers.** The Wheelhouse Assistant answers questions about any part of this document in plain language, without requiring you to find the right section first. It also holds the reference for actions, notices, and configuration settings, so it answers questions this document does not cover. See [Getting Help](#getting-help).

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
- **Using the full command set:** the [Voice Commands](#voice-commands) section, which lists every shipped command and covers formatting and navigation, then [Configuration](#configuration). Every user-facing setting in config.toml is listed in the [configuration reference](https://wheelhouse-project.org/reference.html).
- **Installing, configuring or diagnosing a fault:** [Installation and Setup](wheelhouse_install.md#installation-and-setup), then [Configuration](#configuration), then [Troubleshooting](#troubleshooting). For a question that none of those answer directly, the Wheelhouse Assistant answers from this document in plain language; see [Getting Help](#getting-help).

---

## Speech Engines

The installation guide describes the speech engines: the account each one needs, how they compare, and how to add or switch engines. See [Speech Engines](wheelhouse_install.md#speech-engines) in the installation guide.

### Teaching Wheelhouse your voice

The Distil-Whisper engine sometimes misses short words spoken alone, such as "comma". A short session teaches Wheelhouse how you sound so it stops missing them. Start it by saying "learn my voice" or "calibrate my voice", or right-click either the floating button or the tray icon -- both open the same menu -- open **STT Provider**, and choose **Teach WheelHouse your voice...**.

The session shows four words, one at a time, and asks you to say each word five times. It then asks for a cough or a throat clear, three separate times, to tell your words apart from your other sounds; this part can be skipped. No single step has a time limit, but if twenty minutes pass with no word or sound captured and no button selected, the session cancels itself, saving nothing. While the session is open, spoken words are used for the session only; nothing is typed into any window. The exception is an utterance of "click" followed by at least one more word, which passes to normal processing. A bare "click" or "tap" does not pass: the session takes it, and nothing is typed. Clicking by voice reaches the window's buttons: say "click cancel", with no safety word. "x-ray click cancel" begins with the safety word, so the session discards it.

**Apply** saves what the session learned and restarts the listening, which takes about ten seconds. Only the Distil-Whisper engine uses this teaching; the other engines do not need it. If a different engine is active, the window says so and changes nothing.

---

## System Requirements

The hardware Wheelhouse requires, the hardware it performs well on, and the response times to expect between the two.

### Minimum requirements

- Windows 10 or Windows 11, 64-bit
- A dual-core processor -- Wheelhouse will install and run, but speech recognition may respond slowly; 4 or more cores is the comfortable floor
- 8 GB of RAM for the offline speech engines, Parakeet and Distil-Whisper -- below it, the installer stops for those engines but still installs the Google Cloud engine (in the setup wizard, choose Google Cloud; the one-line command-line installer asks one question when no engine was chosen)
- 6 GB of RAM for any speech engine, including the cloud one -- a hard minimum; below it, the installer stops for every engine, and adding memory is the only fix
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

Wheelhouse converts speech into keystrokes, text, and system actions. Most commands require no prefix. The safety word protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands require **"x-ray"** first. Most rows write the "x-ray" prefix in the first column; a few leave it out. The description of each of those rows says "Needs the safety word first", and "x-ray" is required for them too. A few commands that begin with a common word run without it: "click [name]", "activate [app name]", "switch to [app name]", "show [app name]", "minimize [app name]", "maximize [app name]", and "cancel fix". Saying "x-ray" before one of them still runs it. When such a command cannot do its job -- no control or window has that name, no program starts, Windows will not bring the window forward, or no AI request is running -- and you did not say the safety word, Wheelhouse types the words you said as ordinary text and shows no notice. If you did say the safety word, Wheelhouse shows a notice that explains the failure and types nothing. Two cases differ: "right click [name]" and "double click [name]" show the notice and type nothing even without the safety word, and when any command that names an application ("activate", "switch to", "show", "minimize", "maximize", "x-ray go to", or "x-ray close") starts a program whose window does not come forward in time, or gets no answer in time, Wheelhouse stops without typing anything or showing a notice, with or without the safety word.

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
4. Say **"activate outlook"** (substitute your mail application) to bring its window forward, starting it if it is not already open, then **"submit"** to press Enter.

**Example 2 -- Searching for copied text**

1. Select a phrase with the mouse, or say "select word".
2. Say **"copy"**.
3. Say **"browser"**, as the whole utterance, to bring the default browser forward.
4. Say **"paste"** with the address bar focused, then **"submit"** to press Enter.

### Full Voice Command Reference

Every voice command and replacement is listed below, one row each, in a table under its group heading. The text after each table covers the behavior a table cannot express: how to dictate a word that is also a command, the key names accepted by "press", and how navigation, punctuation, and clicking behave.

#### Dictation Control

| Say this | What happens | Notes |
|---|---|---|
| literal [words] | Types the words after "literal" exactly, skipping all command and replacement processing. Takes effect wherever it appears in an utterance, not only as the first word. | The way to type a word that is also a command -- see "Selected Commands in Detail" in the Voice Commands section |
| insert [text] | Inserts raw text with no capitalization, spacing, or formatting applied | Useful for exact fragments like an email address or a product code |
| no space [words] | Types the words that follow with every space removed | e.g. "no space hello world" types "helloworld" |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item 1", "item 5" |
| submit | Presses Enter. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| submitted | Presses Enter. Fires only when it is the whole utterance. |  |
| type [words] / dictate [words] | Types the words that follow as ordinary dictation, so a phrase that would otherwise run as a command is written out instead | e.g. "type delete all" writes those words rather than clearing the field |
| enter | Presses the Enter key. Fires only when it is the whole utterance. | "submit" does the same |

These commands control what is typed and let you dictate a word that collides with a command. "literal [words]" types the words that follow exactly, bypassing all command and replacement processing. "insert [text]" inserts raw text with no capitalization, spacing, or formatting. "submit" presses Enter when it is the whole utterance.

Utterances beginning with "okay Google", "ok Google", or "hey Google" are discarded, so speech aimed at a nearby voice assistant is not transcribed.

#### Text Editing

| Say this | What happens | Notes |
|---|---|---|
| backspace [number] | Deletes one character to the left, or that many with a number | e.g. "backspace 5" or "backspace twenty three" -- say the count as digits or as words; the number is optional, counts capped at 50. |
| delete [number] | Deletes one character (or that many) to the right. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | e.g. "delete 5" or "delete twenty three" -- say the count as digits or as words; counts capped at 50. Say "erase" in place of "delete" here too, as in "erase 5". |
| delete (or erase) word | Deletes the entire word under the cursor |  |
| undo [number] | Undoes the last action, or several. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Ctrl+Z; e.g. "undo 3". Common mishearings "undue" and "undu" also fire |
| redo [number] | Redoes the last undone action, or several. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Ctrl+Y; the common mishearing "redu" also fires |
| new line | Inserts a line break without submitting the field | Works inline during dictation |
| new paragraph | Inserts two line breaks | Works inline during dictation |
| tab [number] | Presses Tab that many times | e.g. "tab 3" or "tab eleven" -- say the count as digits or as words; "indent 3" does the same. The number is required here; a bare "tab" spoken on its own presses Tab once, and "tab" inside a longer sentence is typed as the word |
| shift tab | Outdents (Shift+Tab) | "outdent" does the same |
| escape | Presses the Escape key. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "dismiss" also works. "dismiss the meeting invite" on its own is typed as ordinary text, not treated as this command. |
| press [keys] [number] times | Presses any key or key combination by name, once, or that many times with "[number] times" | e.g. "press enter", "press alt f4", "press f5", "press tab 3 times" or "press control z two times" -- say the count as digits or as words; the count is optional, counts capped at 30. See the press-keys detail subsection of the Voice Commands section |
| copy | Copies the current selection. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| copy line | Copies the entire current line |  |
| copy all | Copies everything in the current field |  |
| copy screen | Starts the Windows screenshot snipping tool |  |
| cut | Cuts the current selection. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "cut that" also works. Say it as the whole sentence; "cut the vegetables" on its own is typed as ordinary text, not treated as this command. |
| paste | Pastes the clipboard contents. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires the safety word |
| select all | Selects everything in the current field |  |
| delete (or erase) all | Selects everything in the current field and deletes it | Say it as the whole sentence. "delete all the files" on its own is typed as ordinary text, not treated as this command. |
| select word | Selects the word under the cursor | "select this word" also works. "select word by word until it looks right" on its own is typed as ordinary text, not treated as this command. |
| select line | Selects the line under the cursor | "select this line" also works. "select line six and copy it" on its own is typed as ordinary text, not treated as this command. |
| select paragraph | Selects the paragraph under the cursor | "select this paragraph" also works. "select paragraph three of the contract" on its own is typed as ordinary text, not treated as this command. |
| x-ray select [words] | Selects the first place those words appear in the document. The words must match the document exactly, apart from capital letters. | Wheelhouse shows a Windows notification when it selects nothing, so a screen reader can read the reason. |
| save | Saves the current document (Ctrl+S). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| x-ray find [text] | Opens the app's find bar and types the search term | e.g. "x-ray find invoice" |
| replace | Opens find-and-replace (Ctrl+H). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| search | Copies the current selection and runs a web search for it. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Select the text first |
| delete (or erase) next [number] characters | Selects that many characters to the right of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) next [number] words | Selects that many words to the right of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) next [number] lines | Moves to the start of the line, selects down that many lines, and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) next [number] paragraphs | Selects that many paragraphs below the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) previous / last [number] characters | Selects that many characters to the left of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) previous / last [number] words | Selects that many words to the left of the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) previous / last [number] lines | Moves to the start of the line, selects up that many lines, and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) previous / last [number] paragraphs | Selects that many paragraphs above the cursor and deletes them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| delete (or erase) this word | Moves to the start of the word under the cursor, selects the whole word, and deletes it. Fires only when it is the whole utterance. | "delete word" runs the same three keystrokes |
| delete (or erase) line | Selects the whole line the cursor is on and deletes its text. Fires only when it is the whole utterance. | "delete this line" does the same; the line break stays, so the line is left empty |
| delete (or erase) paragraph | Moves to the start of the paragraph the cursor is in, selects the whole paragraph, and deletes it. Fires only when it is the whole utterance. | "delete this paragraph" does the same |
| cut next [number] characters | Selects that many characters to the right of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut next [number] words | Selects that many words to the right of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut next [number] lines | Moves to the start of the line, selects down that many lines, and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut next [number] paragraphs | Selects that many paragraphs below the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] characters | Selects that many characters to the left of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] words | Selects that many words to the left of the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] lines | Moves to the start of the line, selects up that many lines, and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| cut previous / last [number] paragraphs | Selects that many paragraphs above the cursor and cuts them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The cut text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] characters | Selects that many characters to the right of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] words | Selects that many words to the right of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] lines | Moves to the start of the line, selects down that many lines, and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy next [number] paragraphs | Selects that many paragraphs below the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] characters | Selects that many characters to the left of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] words | Selects that many words to the left of the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] lines | Moves to the start of the line, selects up that many lines, and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| copy previous / last [number] paragraphs | Selects that many paragraphs above the cursor and copies them to the clipboard. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. The copied text is left on the clipboard instead of being restored at the end of the utterance |
| select next / forward / right / write [number] characters | Selects that many characters to the right of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select next / forward / right / write [number] words | Selects that many words to the right of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select next / forward / right / write [number] lines | Selects down that many lines from the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select next [number] paragraphs | Selects that many paragraphs below the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select previous / last / backward / left [number] characters | Selects that many characters to the left of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select previous / last / backward / left [number] words | Selects that many words to the left of the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select previous / last / backward / left [number] lines | Selects up that many lines from the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| select previous / last [number] paragraphs | Selects that many paragraphs above the cursor. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| unselect / unselect that / clear selection | Drops the selection with a Right Arrow key press, leaving the cursor at the right-hand end of what was selected. Fires only when it is the whole utterance. | Nothing is deleted; with nothing selected the cursor moves one character right |
| tab | Presses the Tab key once. Fires only when it is the whole utterance. | "tab [number]" presses it that many times |

Common mishearings of "undo" and "redo" ("undue", "undu", "redu") are accepted, so the command still fires on those spellings. Deletion counts for "backspace" and "delete" are capped at 50. "tab [number]" requires the number. A bare "tab" spoken on its own presses Tab once; "tab" inside a longer sentence is typed as the word.

Say "erase" in place of "delete" in any delete command. "erase word", "erase all" and "erase 5" do the same as "delete word", "delete all" and "delete 5". The word "backspace" has no such alternative.

Wherever a command takes **[number]**, say the count either way: as digits ("backspace 15") or as words ("backspace fifteen", "delete twenty three"). Words up to "nine hundred ninety nine" are read, and each command still applies its own limit afterwards.

A spoken count can run to more than one word, so a command with a count waits until no further word could make the number larger, and at the latest until the end of the sentence. "backspace twenty three" deletes twenty-three characters, and "backspace 23" does the same. The wait depends on the number, not on how many words you used: "fifteen" and "ninety nine" are finished as spoken, while "twenty" and "fifty" could still grow into "twenty three" or "fifty five", so those wait for the next word. Every count command behaves this way, including "backspace". Chained "go" and "grab" moves, and moves with neither a unit word nor "times", read their counts differently, one word at a time; see the paragraph on counts further down this section.

##### The "press [keys]" Command in Detail

"press [keys]" sends any keyboard shortcut. Modifiers are held down first regardless of spoken order, so "press delete control" equals "press control delete". If any word in the phrase is unrecognized, nothing is pressed, and the whole phrase, "press" included, is typed as dictation. Hyphenated tokens from the speech engine, such as "f-11" or "control-alt", are split automatically.

**Modifier keys**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: f1 through f12. The letter and the number can be separate words: "press f 5" and "press f five" both press F5.

**Letters**: any single letter a through z.

**Digits**: 0 through 9 are key names, so "press control 2" presses Ctrl+2. Right after "f", a number from 1 to 12 names a function key instead; "press f 0" presses F and 0 together.

**Repeat count**: end the phrase with "[number] times" to press the keys that many times: "press tab 3 times", "press control z two times", "press down arrow twenty times". Say the count as digits or as words; counts are capped at 30, and "1 time" is accepted. The word "times" is required: without it a trailing number is part of the keys, so "press control 2" presses Ctrl+2 once and "press f 5" presses F5 once.

**Symbols by spoken name**: the following are pressed correctly -- backtick, semicolon, slash (forward slash), backslash (back slash), comma, period (dot), single quote (apostrophe), left/right bracket (open/close bracket), equals (equal), minus (hyphen, dash), and the parentheses: left parenthesis (left paren, open parenthesis, open paren) presses Shift+9, and right parenthesis (right paren, close parenthesis, close paren) presses Shift+0, which type "(" and ")" on a US keyboard layout. Other symbol names are not reliable in "press": the shifted symbols (colon, tilde, pipe, question mark, double quote, braces, less than, greater than, plus, underscore) produce the wrong character, and hash, at, ampersand, asterisk, caret, percent, dollar, and exclamation press nothing. To type any of those, dictate them as punctuation words instead; [Punctuation and Symbols](#punctuation-and-symbols) below handles every symbol.

**Examples**: "press control shift t", "press f5", "press alt f4", "press windows d", "press left bracket".

#### Text Formatting

| Say this | What happens | Notes |
|---|---|---|
| uppercase | Converts the selection to UPPERCASE. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "upper case" and "uppercase that" also work |
| all caps / all caps that | Converts the selection to UPPERCASE. Applies only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. |  |
| lowercase | Converts the selection to lowercase. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "lower case" and "lowercase that" also work |
| no caps / no caps that | Converts the selection to lowercase. Applies only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. |  |
| capitalize | Capitalizes the first letter of the selection and lowercases the rest. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "capitalize that" also works |
| cap / cap that | Capitalizes the first letter of the selection and lowercases the rest. Applies only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. |  |
| title case | Converts the selection to Title Case |  |
| snake case | Converts the selection to snake_case |  |
| camel case | Converts the selection to camelCase |  |
| pascal case | Converts the selection to PascalCase |  |
| kebab case | Converts the selection to kebab-case |  |
| compress | Removes the spaces from the selection, joining the words together. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| no space / no space that | Removes the spaces from the selected text, joining the words together. Fires only when it is the whole utterance. | Acts on the selected text only; with nothing selected it does not act on the last dictated words |
| bold | Bolds the selection (Ctrl+B). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "bold text", "bold that", "boldface", "boldface that", "bold face", and "bold face that" also work. Works in apps that support rich text |
| italics | Italicizes the selection (Ctrl+I). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "italicize" and "italicize that" also work. Works in apps that support rich text |
| underline | Underlines the selection (Ctrl+U). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "underline that" also works. Works in apps that support rich text |
| parentheses [text] | Wraps the selection in ( ), inserts an empty ( ) pair, or inserts the spoken text wrapped | "parentheses hello" gives "(hello)" |
| brackets [text] | Wraps the selection in [ ], inserts an empty pair, or wraps the spoken text |  |
| braces [text] | Wraps the selection in { }, inserts an empty pair, or wraps the spoken text |  |
| angle brackets [text] | Wraps the selection in < >, inserts an empty pair, or wraps the spoken text |  |
| quotes [text] | Wraps the selection in double quotes, inserts an empty pair, or wraps the spoken text |  |
| single quotes [text] | Wraps the selection in single quotes, inserts an empty pair, or wraps the spoken text |  |
| bold next [number] characters | Selects that many characters to the right of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold previous / last [number] characters | Selects that many characters to the left of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold next [number] words | Selects that many words to the right of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold previous / last [number] words | Selects that many words to the left of the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold next [number] lines | Selects down that many lines from the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold previous / last [number] lines | Selects up that many lines from the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold next [number] paragraphs | Selects that many paragraphs below the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| bold previous / last [number] paragraphs | Selects that many paragraphs above the cursor and makes them bold. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize next [number] characters | Selects that many characters to the right of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize previous / last [number] characters | Selects that many characters to the left of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize next [number] words | Selects that many words to the right of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize previous / last [number] words | Selects that many words to the left of the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize next [number] lines | Selects down that many lines from the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize previous / last [number] lines | Selects up that many lines from the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize next [number] paragraphs | Selects that many paragraphs below the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| italicize previous / last [number] paragraphs | Selects that many paragraphs above the cursor and makes them italic. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline next [number] characters | Selects that many characters to the right of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline previous / last [number] characters | Selects that many characters to the left of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline next [number] words | Selects that many words to the right of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline previous / last [number] words | Selects that many words to the left of the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline next [number] lines | Selects down that many lines from the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline previous / last [number] lines | Selects up that many lines from the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline next [number] paragraphs | Selects that many paragraphs below the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| underline previous / last [number] paragraphs | Selects that many paragraphs above the cursor and underlines them. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. Works in apps that support rich text |
| uppercase next [number] characters | Selects that many characters to the right of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase previous / last [number] characters | Selects that many characters to the left of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase next [number] words | Selects that many words to the right of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase previous / last [number] words | Selects that many words to the left of the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase next [number] lines | Selects down that many lines from the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase previous / last [number] lines | Selects up that many lines from the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase next [number] paragraphs | Selects that many paragraphs below the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| uppercase previous / last [number] paragraphs | Selects that many paragraphs above the cursor and converts them to UPPERCASE. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "upper case" also works |
| lowercase next [number] characters | Selects that many characters to the right of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase previous / last [number] characters | Selects that many characters to the left of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase next [number] words | Selects that many words to the right of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase previous / last [number] words | Selects that many words to the left of the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase next [number] lines | Selects down that many lines from the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase previous / last [number] lines | Selects up that many lines from the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase next [number] paragraphs | Selects that many paragraphs below the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| lowercase previous / last [number] paragraphs | Selects that many paragraphs above the cursor and converts them to lowercase. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "lower case" also works |
| capitalize next [number] characters | Selects that many characters to the right of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize previous / last [number] characters | Selects that many characters to the left of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize next [number] words | Selects that many words to the right of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize previous / last [number] words | Selects that many words to the left of the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize next [number] lines | Selects down that many lines from the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize previous / last [number] lines | Selects up that many lines from the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize next [number] paragraphs | Selects that many paragraphs below the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| capitalize previous / last [number] paragraphs | Selects that many paragraphs above the cursor and capitalizes the first letter of the selection and lowercases the rest. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |

Formatting commands apply to the current selection: select first, with the mouse or "select word" / "select line", then say the command. The case and shape transforms cover UPPERCASE, lowercase, capitalize, title case, and the programming styles snake_case, camelCase, PascalCase, and kebab-case. The wrapping commands ("parentheses", "brackets", "braces", "angle brackets", "quotes", "single quotes") enclose the selection in those characters; spoken with no selection, they insert an empty pair with the cursor between them. Words spoken after a wrapping word in the same utterance are wrapped verbatim: symbol words such as "colon" are typed literally rather than converted. Three commands apply character formatting through the host application's own keyboard shortcuts: "bold text", "italics", and "underline". None of the three needs the safety word, and each fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed. Forty-eight more commands select a range and format it in one step: "bold", "italicize", "underline", "uppercase", "lowercase", or "capitalize", then "next" or "previous" ("last" also works), an optional count, and a unit -- words, lines, paragraphs, or characters. For example, "bold next 3 words" or "underline previous line". These also need no safety word and fire only as your whole utterance.

#### Navigation

| Say this | What happens | Notes |
|---|---|---|
| go [where] | Moves the cursor without touching the keyboard: go home / go end / go top / go bottom / go left / go right, with counts, word and paragraph units, and "then"-chained "grab" steps that select along the way | See the text below the Navigation table for the full move list |
| go up [number] lines / move up [number] lines | Moves the cursor up that many lines. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go down [number] lines / move down [number] lines | Moves the cursor down that many lines. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go up [number] paragraphs / move up [number] paragraphs | Moves the cursor back that many paragraphs (Ctrl and Up Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go down [number] paragraphs / move down [number] paragraphs | Moves the cursor forward that many paragraphs (Ctrl and Down Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go left [number] characters / move left [number] characters | Moves the cursor left that many characters. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "move backward [number] characters" and "go backward [number] characters" also work |
| go right [number] characters / move right [number] characters | Moves the cursor right that many characters. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "move forward [number] characters" and "go forward [number] characters" also work. "go write [number] characters" and "move write [number] characters" also work, because speech recognition can hear "right" as "write" |
| go left [number] words / move left [number] words | Moves the cursor back that many words (Ctrl and Left Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go right [number] words / move right [number] words | Moves the cursor forward that many words (Ctrl and Right Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "write" in place of "right" also works, because speech recognition can hear "right" as "write" |
| go up [number] times / move up [number] times | Presses the Up Arrow key that many times. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go down [number] times / move down [number] times | Presses the Down Arrow key that many times. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go left [number] times / move left [number] times | Presses the Left Arrow key that many times. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go right [number] times / move right [number] times | Presses the Right Arrow key that many times. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "write" in place of "right" also works, because speech recognition can hear "right" as "write" |
| move slider up [number] times / go slider up [number] times | Presses the Up Arrow key that many times, for moving a focused slider. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| move slider down [number] times / go slider down [number] times | Presses the Down Arrow key that many times, for moving a focused slider. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| move slider left [number] times / go slider left [number] times | Presses the Left Arrow key that many times, for moving a focused slider. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| move slider right [number] times / go slider right [number] times | Presses the Right Arrow key that many times, for moving a focused slider. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "write" in place of "right" also works, because speech recognition can hear "right" as "write" |
| go to home / go to the beginning of the line | Moves the cursor to the start of the current line (Home). Fires only when it is the whole utterance. | "go to the start of the line" also works. "move to the beginning of the line" also works ("move to home" does not) |
| go to the top / go to the top of the document | Moves the cursor to the very start of the document (Ctrl+Home). Fires only when it is the whole utterance. | "go to the beginning of the document" and "go to the start of the document" also work. "move to the top" and "move to the beginning of the document" also work ("move to the top of the document" does not) |
| go to the bottom / go to the bottom of the document | Moves the cursor to the very end of the document (Ctrl+End). Fires only when it is the whole utterance. | "go to the end of the document" also works. "move to the bottom" and "move to the end of the document" also work ("move to the bottom of the document" does not) |
| go to the end of the line / go to the end | Moves the cursor to the end of the current line (End). Fires only when it is the whole utterance. | "move to the end of the line" also works ("move to the end" does not) |
| go to the beginning of the word | Moves the cursor back one word (Ctrl and Left Arrow). Fires only when it is the whole utterance. | "go to the start of the word" also works; each app decides where a word boundary falls. "move to" works as well as "go to" |
| go to the end of the word | Moves the cursor forward one word (Ctrl and Right Arrow). Fires only when it is the whole utterance. | Each app decides where a word boundary falls, so the cursor may land at the start of the next word. "move to" works as well as "go to" |
| go to the beginning of the paragraph | Moves the cursor back one paragraph (Ctrl and Up Arrow). Fires only when it is the whole utterance. | "go to the start of the paragraph" also works. "move to" works as well as "go to" |
| go to the end of the paragraph | Moves the cursor forward one paragraph (Ctrl and Down Arrow). Fires only when it is the whole utterance. | Each app decides where a paragraph boundary falls, so the cursor may land at the start of the next paragraph. "move to" works as well as "go to" |
| move to the beginning of the selection / go to the beginning of the selection | Presses Left Arrow, which drops the selection and leaves the cursor at its left-hand end. Fires only when it is the whole utterance. | With nothing selected the cursor moves one character left |
| move to the end of the selection / go to the end of the selection | Presses Right Arrow, which drops the selection and leaves the cursor at its right-hand end. Fires only when it is the whole utterance. | With nothing selected the cursor moves one character right |
| scroll down [number] | Turns the mouse wheel down, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The number is how many wheel notches to send, up to 50; e.g. "scroll down 3". The count can be digits or spoken words, so "scroll down eleven" works whether your speech provider writes numbers as digits or as words. The command turns the wheel without moving the pointer, so it scrolls whatever a real wheel turn would scroll from where the pointer already sits |
| scroll up [number] | Turns the mouse wheel up, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The number is how many wheel notches to send, up to 50; e.g. "scroll up 3". The count can be digits or spoken words, so "scroll up eleven" works whether your speech provider writes numbers as digits or as words. |
| scroll left [number] | Turns the sideways mouse wheel left, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. The number is how many wheel notches to send, up to 50. The count can be digits or spoken words, so "scroll left eleven" works whether your speech provider writes numbers as digits or as words. |
| scroll right [number] | Turns the sideways mouse wheel right, without moving or pressing the mouse. Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. The number is how many wheel notches to send, up to 50. The count can be digits or spoken words, so "scroll right eleven" works whether your speech provider writes numbers as digits or as words. "write" in place of "right" also works, because speech recognition can hear "right" as "write". |
| start scrolling down | Keeps turning the mouse wheel down until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The scroll keeps going with no further speech, so you can read a long page hands-free. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so, in case the stop command is not heard. If Windows refuses to move the wheel, which can happen over a window that runs as administrator, the scroll stops early and shows a different notification. Any scroll command you say while it runs replaces it, so only one scroll is ever going. |
| start scrolling up | Keeps turning the mouse wheel up until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The scroll keeps going with no further speech. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| start scrolling left | Keeps turning the sideways mouse wheel left until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| start scrolling right | Keeps turning the sideways mouse wheel right until you say "stop scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | Needs an application that reads the sideways wheel; many do not. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. "write" in place of "right" also works, because speech recognition can hear "right" as "write". |
| stop scrolling | Stops a scroll that was started with "start scrolling". Fires only when the phrase is the whole utterance; inside a longer sentence the words are typed normally. | The words are "stop scrolling" and not "stop" on its own. A command for the single word "stop" would take that word out of everything you dictate. Saying it when nothing is scrolling does nothing at all. |

"go" moves the cursor; "grab" moves it while extending the selection. Several moves chain in one utterance with "then". The utterance must begin with "go": "grab" is valid only chained after a "go" move -- "go home then grab to end". Spoken on its own, "grab ..." is typed as dictation.

Counts can be digits ("3") or spoken words ("one", "fifteen", "twenty three"). A single move that names a unit accepts a count of up to five words: "go right twenty three words", "go up 3 lines", "go left 5 characters", "go down 2 paragraphs". In a chained move, and in a move with neither a unit word nor "times", a count is one word, so say "twenty-three" there as a single hyphenated word rather than two separate words. Counts above 30 move 30. In a chained move, and in a move with neither a unit word nor "times", a digit count above 999 is typed as dictation instead of moving the cursor; a single move with a unit word or "times" moves 30 for such a count. Leading zeroes do not count, so "0001" still moves one. "to", "too", and "for" are accepted as sound-alikes for 2 and 4, so "go right to words" moves two words. If any part of a "go" utterance cannot be parsed, the whole phrase is typed as dictation instead, so an unrecognized phrase does not move the cursor.

#### Punctuation and Symbols

| Say this | What happens | Notes |
|---|---|---|
| period | Types . inline during dictation | "full stop" also works |
| comma | Types , inline during dictation |  |
| colon | Types : inline during dictation |  |
| semicolon | Types ; inline during dictation |  |
| question mark | Types ? inline during dictation |  |
| exclamation point | Types ! inline during dictation | "exclamation mark" also works |
| apostrophe | Types ' inline during dictation |  |
| hyphen | Types - inline during dictation | "minus sign" also works |
| dash | Types an em dash (the long dash) inline during dictation |  |
| slash | Types / inline during dictation | "forward slash" also works |
| backslash | Types \\ inline during dictation |  |
| backtick | Types \` inline during dictation |  |
| at sign | Types @ inline during dictation |  |
| hashtag | Types # inline during dictation | "number sign" and "pound sign" also work |
| dollar sign | Types $ inline during dictation |  |
| percent | Types % inline during dictation |  |
| caret sign | Types ^ inline during dictation | Also fires if heard as "carrot sign" |
| ampersand | Types & inline during dictation | Say "ampersand"; "and sign" types the words |
| asterisk | Types * inline during dictation |  |
| underscore | Types _ inline during dictation |  |
| plus sign | Types + inline during dictation, with a space on each side: "a plus sign b" types "a + b" | No space goes before + at the start of a line, after a space, or right after one of = + < > \| ! * - % ^ & : ~ ?, so "plus sign plus sign" types ++ |
| equal sign | Types = inline during dictation, with a space on each side: "x equal sign y" types "x = y" | Also fires if heard as "equals sign", "equal sine", or "equals sine". Because of that, dictating "equals sine" types = instead of the word sine. No space goes before = at the start of a line, after a space, or right after one of = + < > \| ! * - % ^ & : ~ ?, so "less than sign equal sign" types <= and "exclamation mark equal sign" types != |
| tilde | Types ~ inline during dictation | Also fires if heard as "tilda" |
| vertical bar | Types the pipe character inline during dictation, with a space on each side, as in "a \| b" | "pipe character" also works. No space goes before the pipe character at the start of a line, after a space, or right after one of = + < > \| ! * - % ^ & : ~ ?, so "vertical bar vertical bar" types two pipe characters together |
| ellipsis | Types ... inline during dictation | "dot dot dot" also works |
| space bar | Types a single literal space inline during dictation |  |
| open bracket | Types [ inline during dictation |  |
| close bracket | Types ] inline during dictation |  |
| open brace | Types { inline during dictation | "left brace" also works |
| close brace | Types } inline during dictation | "right brace" also works |
| open parentheses | Types ( inline during dictation | "left parentheses" also works |
| close parentheses | Types ) inline during dictation | "right parentheses" also works |
| open quotes | Types a double quote inline during dictation |  |
| close quotes | Types a double quote inline during dictation |  |
| open single quote | Types a single quote inline during dictation | "begin single quote" also works |
| close single quote | Types a single quote inline during dictation | "end single quote" also works |
| less than sign | Types < inline during dictation, with a space on each side: "a less than sign b" types "a < b" | No space goes before < at the start of a line, after a space, or right after one of = + < > \| ! * - % ^ & : ~ ?, so "less than sign equal sign" types <= |
| greater than sign | Types > inline during dictation, with a space on each side: "a greater than sign b" types "a > b" | No space goes before > at the start of a line, after a space, or right after one of = + < > \| ! * - % ^ & : ~ ?, so "greater than sign greater than sign" types >> |
| euro sign | Types € inline during dictation |  |
| yen sign | Types ¥ inline during dictation |  |
| pound sterling sign | Types £ inline during dictation |  |
| copyright sign | Types © inline during dictation |  |
| registered sign | Types ® inline during dictation |  |
| section sign | Types § inline during dictation |  |
| paragraph sign | Types ¶ inline during dictation | "paragraph mark" also works |
| degree symbol | Types ° inline during dictation |  |
| multiplication sign | Types × inline during dictation |  |
| division sign | Types ÷ inline during dictation |  |
| colin | Mishear tolerance: inserts : when "colin" is the entire utterance. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| come | Mishear tolerance: inserts , when "come", "kama", "commer", or "come on" is the entire utterance. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |

Punctuation and symbol words are replacements: spoken as part of a sentence, the symbol is typed in place of the word, no pause required. Every punctuation and symbol word -- period, comma, colon, question mark, and the rest -- behaves this way.

Two mishearing tolerances are built in, because the default local engine frequently mishears "comma" and "colon": spoken as an entire utterance, **"colin"** inserts ":" and **"come"**, **"kama"**, **"commer"**, or **"come on"** inserts ",". Within a longer sentence these words are dictated normally. To type one as a standalone word, use "literal come" or "literal colin".

If the recognizer regularly mishears another word, add a personal correction in the Pattern Manager ("patterns"); it applies inline like the built-in punctuation words.

#### Application Switching and System

| Say this | What happens | Notes |
|---|---|---|
| activate [app name] | Brings the named application's window forward; when nothing by that name is open, Wheelhouse looks the name up among your installed programs and starts it. Needs no prefix word. When the command cannot be done (no window matches and no program starts, or Windows will not bring the window forward), Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows a notice that explains the failure and types nothing. When Wheelhouse starts a program and its window does not come forward in time, or the wait gets no answer in time, it stops without typing anything or showing a notice, with or without "x-ray". | e.g. "activate outlook"; the spoken name must match whole words of a window title; when more than one installed program matches the name, Wheelhouse starts none of them; with "x-ray" first, a notice lists them so you can say the full name, and without it your words are typed as ordinary text |
| browser | Brings your default web browser to the front. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Wheelhouse looks up which browser is your Windows default at the moment you speak |
| notepad | Brings Notepad to the front. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| switch to [app name] / show [app name] | Brings the named application's window forward, starting it when nothing by that name is open. Needs no prefix word. When the command cannot be done (no window matches and no program starts, or Windows will not bring the window forward), Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows a notice that explains the failure and types nothing. When Wheelhouse starts a program and its window does not come forward in time, or the wait gets no answer in time, it stops without typing anything or showing a notice, with or without "x-ray". | e.g. "switch to outlook"; the same action as "activate [app name]", including the program lookup; the spoken name must match whole words of a window title, so "show art" brings forward "Art class - Word" and not "Start menu" |
| x-ray go to [app name] | Brings the named application's window forward, starting it when nothing by that name is open. Needs the safety word first. When Wheelhouse starts a program and its window does not come forward in time, or the wait gets no answer in time, it stops without typing anything or showing a notice. | e.g. "x-ray go to outlook"; the same action as "activate [app name]", including the program lookup. Without "x-ray", "go to" stays a cursor-movement command, so "go to end of line" needs no prefix word |
| close [app name] / exit [app name] / quit [app name] | Brings the named application's window forward and then closes it (Alt+F4). Needs the safety word first. When Wheelhouse starts a program and its window does not come forward in time, or the wait gets no answer in time, it stops without typing anything or showing a notice. | Destructive -- requires the safety word. When no window matches and no program starts, or Windows will not bring the window forward, a notice explains why and nothing is closed or typed |
| minimize [app name] | Brings the named application's window forward and then sends Windows and Down Arrow. Needs no prefix word. When no window matches and no program starts, or Windows will not bring the window forward, no key is sent, and Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows a notice that explains the failure and types nothing. When no window matches, Wheelhouse starts the program and sends the key once its window comes forward; if the new window does not come forward in time, Wheelhouse stops without sending the key, typing anything, or showing a notice. | That one keystroke has two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. The spoken name must match whole words of a window title. |
| maximize [app name] | Brings the named application's window forward and then maximizes it (Windows and Up Arrow). Needs no prefix word. When no window matches and no program starts, or Windows will not bring the window forward, no key is sent, and Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows a notice that explains the failure and types nothing. When no window matches, Wheelhouse starts the program and sends the key once its window comes forward; if the new window does not come forward in time, Wheelhouse stops without sending the key, typing anything, or showing a notice. | The spoken name must match whole words of a window title. |

| Say this | What happens | Notes |
|---|---|---|
| zoom in | Zooms in (Ctrl and plus) |  |
| zoom out | Zooms out (Ctrl and minus) |  |
| create tab | Sends Ctrl+N | New tab in most editors; note that in most browsers Ctrl+N opens a new window, not a tab |
| create window | Sends Ctrl+Shift+N | New window in editors; opens a private/incognito window in most browsers |
| x-ray close window | Closes the active window (Alt+F4) | Requires the safety word |
| maximize | Maximizes the active window. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| minimize | Sends Windows and Down Arrow to the active window. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | That one keystroke has two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. "restore window" sends the same keystroke. |
| desktop | Shows the desktop (Windows+D). Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. |  |
| Windows settings | Opens the Windows Settings app | Also fires if heard as "Window settings" |
| close tab | Closes the current tab (Ctrl+W). Needs the safety word first. | What Ctrl+W closes is decided by the app |
| restore window | Sends Windows and Down Arrow to the active window. Fires only when it is the whole utterance. | The same keystroke as "minimize", so it has the same two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. |
| snap window to the left | Snaps the active window to the left half of the screen (Windows and Left Arrow). Fires only when it is the whole utterance. | "the" is optional: "snap window to left" also works |
| snap window to the right | Snaps the active window to the right half of the screen (Windows and Right Arrow). Fires only when it is the whole utterance. | "the" is optional: "snap window to right" also works. "write" in place of "right" also works, because speech recognition can hear "right" as "write" |
| snap window to the top | Snaps the active window to the top of the screen (Windows, Alt and Up Arrow). Fires only when it is the whole utterance. | "the" is optional: "snap window to top" also works |
| snap window to the bottom | Snaps the active window to the bottom of the screen (Windows, Alt and Down Arrow). Fires only when it is the whole utterance. | "the" is optional: "snap window to bottom" also works |
| minimize all windows | Minimizes every open window (Windows+M). Fires only when it is the whole utterance. | "minimize all" does the same |
| show task switcher / list all windows / show all windows | Opens Task View, the Windows overview of every open window (Windows+Tab). Fires only when it is the whole utterance. |  |
| keyboard | Toggles the Windows On-Screen Keyboard (Windows, Ctrl and O). Fires only when it is the whole utterance. | One word, both directions: it opens the keyboard when closed and closes it when open. The earlier show and hide phrasings were removed because they promised a direction the toggle cannot deliver |
| search windows for [words] | Opens Windows Search and types what you say into it. Needs the safety word first. |  |
| search on google for [words] / search for [words] | Opens a Google search for the words you say in your default browser. Needs the safety word first. | "search [words]" without "for" does the same |
| search on bing for [words] | Opens a Bing search for the words you say in your default browser. Needs the safety word first. |  |
| search on youtube for [words] | Opens a YouTube search for the words you say in your default browser. Needs the safety word first. |  |

"activate [app name]" brings the named application's window forward. When nothing by that name has a window open, Wheelhouse starts the program instead of doing nothing: a pattern whose target is a program file (.exe) is run directly, and a spoken name is looked up among your installed programs, by Start menu shortcut and by the names Windows itself resolves. Programs installed from the Microsoft Store are found too, by the name the Start menu shows or the name the Store lists, for example "terminal" or "windows terminal". A Store program that is already running is brought forward instead of started again, even when its window title does not contain the name. An exact name is preferred; failing that, a name that begins with the words you said. If more than one installed program matches, or none matches, Wheelhouse starts nothing, types the words you said as ordinary text, and shows no notice. If you said the safety word first, Wheelhouse types nothing and shows a notice instead: the notice lists the matching names, so you can say the full name of the one you meant, or says that no program matched. The spoken name must match whole words of a window title, so "show art" brings forward a window titled "Art class - Word" and not one titled "Start menu". "switch to [app name]", "show [app name]", "minimize [app name]", and "maximize [app name]" work the same way, need no safety word, and type your words in the same cases when they cannot be done. "x-ray go to [app name]" and "x-ray close [app name]" (also "exit" and "quit") need the safety word: "go to" also begins the cursor-movement commands such as "go to end of line", which need none, and closing can discard unsaved work. When any of these commands starts a program because no window matched, Wheelhouse waits for the new window to come forward; if the window does not come forward in time, or the wait gets no answer in time, Wheelhouse stops without typing anything or showing a notice, with or without the safety word. "minimize", "maximize", and "close" send their key only after the new window comes forward, so they send no key in that case. The built-in "notepad" and "browser" take the program-file path, and "browser" resolves to the Windows default browser when spoken. The System commands operate on windows and on Windows itself. Seven need the safety word: "x-ray close window", because it discards unsaved work; "x-ray close tab"; the four searches, "x-ray search windows for [text]", "x-ray search on google for [text]", "x-ray search on bing for [text]", and "x-ray search on youtube for [text]"; and the short Google form "x-ray search for [text]", in which the word "for" is optional. Every other System command works without the safety word, and each of those fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed. Those include "zoom in", "zoom out", "create tab", "create window", "windows settings", which opens Windows Settings, "maximize", "minimize", and "desktop", which shows the desktop. In most browsers "create tab" (Ctrl+N) opens a new window rather than a tab, and "create window" (Ctrl+Shift+N) opens a private or incognito window.

#### Mouse Control

| Say this | What happens | Notes |
|---|---|---|
| show grid | Lays a numbered three-by-three grid over the screen the front window is on, for moving the pointer by voice | Opening the grid removes the numbered overlay; the two are never on screen together. "apply grid" also works. "show grid lines on the chart" is not this command: it is read as "show" plus a window name, and when no window has those words in its title it is typed as ordinary text. See the text below the Mouse Control table |
| hide grid | Closes the grid without clicking anything | "dismiss grid" also works. "hide grid lines before printing" on its own is typed as ordinary text, not treated as this command. |
| grid next screen | Moves the open grid to the next monitor, restarted at full size |  |
| [number 1-9] | Redraws the open grid inside that cell, narrowing the target. With the grid closed and the numbered overlay showing, the number clicks the control with that label. With neither showing, numbers are ordinary dictation and are typed normally. |  |
| number [1-9] | Same cell narrowing with the word "number" first ("number five"), which speech engines hear more reliably than a single word. With the grid closed and the numbered overlay showing, the phrase clicks the control with that label. With neither showing, the phrase is ordinary dictation and is typed normally. |  |
| click / tap / right click / double click / triple click | Clicks at the center of the grid's current cell and closes the grid; a number in the same utterance narrows first ("click 5"). With no grid open, the words click at the current pointer position; a triple click there selects a line of text. Say "literal click" to type the word. When voice clicking is turned off, the words are typed normally. |  |
| mark | Pins the start point of a drag at the current cell center and restarts the grid so you can navigate to the destination |  |
| drag | Holds the left button at the marked point, moves gradually to the current cell center, and releases -- a complete drag and drop | Requires a "mark" first; without one, a notice explains the step |
| move here / go here | Moves the pointer to the current cell center without pressing anything, for hover menus and tooltips |  |

The **mouse grid** moves the pointer to any point on screen by voice, including points clicking by name cannot reach: applications that hide their controls from accessibility tools, drawing canvases, maps, games. Say **"show grid"** to lay a numbered three-by-three grid over the screen the front window is on. Saying a cell's number, **1 through 9**, redraws the grid inside that cell; repeat until the center sits on the target. The word "number" may precede each number -- engines hear "number five" more reliably than a single word. Then say the action:

| Say | What happens |
|---|---|
| click | left click at the center of the current cell |
| double click | double click there |
| triple click | triple click there, which selects a line of text |
| right click | right click there |
| move here / go here | moves the pointer there without clicking |
| mark | pins the start point of a drag and restarts the grid |
| drag | drags from the pinned point to the current cell center |
| hide grid | closes the grid without clicking |
| grid next screen | moves the grid to the next monitor |

A number in the same utterance narrows first: "click 5" narrows into cell 5, then clicks its center. All actions close the grid except "mark", "grid next screen", and a "drag" with no mark.

**With no grid open**, "click", "tap", "right click", "double click", and "triple click" spoken as your whole utterance click at the pointer's current position. Inside a longer sentence the words are typed. To type one of them on its own, say "literal click". When voice clicking is turned off (`enabled` in the click section of the settings file), the words are typed.

**Dragging** is two steps. Navigate to the point to drag from and say **"mark"** -- a pin appears and the grid restarts at full size. Navigate to the destination and say **"drag"**: the left button is pressed at the pin, the pointer moves gradually -- many applications ignore a pointer that jumps instantly -- and the button is released. No button is held while you navigate, so a pause or a misheard word between the steps cannot drop anything in the wrong place. "drag" without a "mark" shows a notice and presses nothing; the pin is forgotten when the grid closes.

While the grid is open, a spoken number on its own belongs to the grid and is not typed. With the grid closed and the numbered overlay showing, a number spoken as your whole utterance clicks the control with that label. With neither showing, the same words are ordinary dictation. The grid and the numbered overlay are never open together: "show grid" removes the numbers, "show numbers" closes the grid. Narrowing stops once cells reach `grid_min_cell_px` (click section of the settings file) -- a cell that small is already within a click's precision -- and drag speed is `drag_duration_ms` next to it.

No commands move the pointer continuously (no "mouse up" / "mouse down"); the grid places it at a chosen point instead. Volume and screen brightness are mapped to the thumb wheel of a Logitech MX-series mouse; see [Plugins](#plugins).

#### Voice Element Clicking

| Say this | What happens | Notes |
|---|---|---|
| click [name] | Clicks the button, link, menu item, or other control with that name; add a role word to narrow the search, or give the overlay number instead of a name while the numbered overlay is showing. Needs no prefix word: say "click cancel". "clicks" and "tap" work too. When no control has that name, Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows the "No match" notice and types nothing. | See the text below the Voice Element Clicking table |
| show numbers | Paints a number on every clickable control in the front window | Numbers stay up until you say "hide numbers". "apply numbers" and "show numbers here" also work. "show numbers in the report" is not this command: it is read as "show" plus a window name, and when no window has those words in its title it is typed as ordinary text. |
| hide numbers | Removes the numbers | "dismiss numbers" also works. "hide numbers on the chart" on its own is typed as ordinary text, not treated as this command. |
| right click [name] / double click [name] | Clicks the named control with a right click or a double click instead of a normal click; also works with a number while the numbered overlay is showing ("right click 3"). Presses a real mouse click at the control's center, with the same checks as a normal click. Useful where a normal click is not enough: File Explorer items open on a double click, and a right click opens the context menu. |  |

Wheelhouse can click buttons, links, menu items, and other on-screen controls. A control is selected in one of two ways: by its **name**, or by displaying a **number** on every clickable control and clicking that number -- "click 5". The numbered overlay covers controls with no spoken name, such as icon-only toolbar buttons, and cases where several controls share a name.

**Clicking by name**: say "click", then the name of the control. "the" may precede the name and is ignored; a role word may follow to restrict the search to one kind of control. Plain "click [name]" needs no safety word -- say "click cancel"; "clicks" and "tap" work the same way. Because any sentence that opens with "click" or "tap" reaches this command, Wheelhouse looks for the control first: when no control has that name, it types the words you said as ordinary text and shows no notice. If you say the safety word first, as "x-ray click cancel", it shows the "No match" notice instead and types nothing. "right click [name]" and "double click [name]" need no safety word. **Role words**: **button**, **link** (a hyperlink), **menu** (a menu item), **tab**, **checkbox** (or **check box**), and **box** / **field** / **input** (a text entry field). With no role word, any clickable control matching the name is considered. A role word spoken with no name -- "click button" -- is treated as a name and searches for a control named "button".

**Right click and double click**: "right click [name]" and "double click [name]" work wherever "click [name]" works, and "right click 3" / "double click 3" work while the numbered overlay is showing. These press a real mouse click at the control's center, with the same safety checks as a normal click. They cover what a normal click cannot: a right click opens a control's context menu, and File Explorer items open on a double click.

**The numbered overlay**: "show numbers" displays a number on every clickable control in the front window, "click 3" clicks the control labelled 3, and "hide numbers" removes the numbers. Until then they stay up: clicking a numbered control repaints them in place, and they follow whichever window is in front. When "click [name]" matches more than one control closely, the numbers appear on those candidates only. With the mouse grid closed, a number spoken as your whole utterance ("7", "number seventy four") also clicks the control with that label; while the grid is open, a bare number belongs to the grid. A control whose own name is a digit -- a calculator "7" -- needs a role word: "click 7 button" clicks the calculator key rather than the control labelled 7. If the numbers no longer align after a page scrolls or changes, say "show numbers" again. Over the Pattern Manager the numbers follow its list: when you filter, add, remove, edit, expand, or collapse rows, Wheelhouse numbers the list again, and clicking the number of a pattern or category row selects that row and shows its details.

**Outcomes**: a successful click produces no notice. A failure produces a brief notice near the floating button and the tray icon: **not found** ("No match for [name]", shown only when you said the safety word first -- otherwise the words are typed as ordinary text; the numbered overlay is the alternative), **ambiguous** (the numbered overlay opens on the candidates; the "Found [A] and [B] -- be more specific" notice appears only when the overlay cannot open), and **could not complete the click** (the wording states the reason: control disabled, click timed out, or overlay stale and must be reapplied). Notices are rate-limited, so repeated failures do not produce repeated notices.

#### Wheelhouse Control

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening: Wheelhouse listens only while you hold the floating button | A notification confirms the switch |
| click to talk mode | Switches back to toggle listening (click to start, click to stop) -- the default |  |
| stop listening | Switches listening off, as clicking the floating button or the tray icon does while it listens. Applies only when the words are the whole utterance; inside a longer sentence they dictate normally. | In toggle mode, say the wake word ("computer") to switch listening back on. In push-to-talk mode the wake word ends only the idle pause, so the way back is the next hold of the floating button. A notice confirms that listening is off |
| help | Opens the Wheelhouse Assistant (the official online help) in your browser, after a short explanation window. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "open voice access help" also opens it. Uses the assistant_url setting under [ai.help]; if blanked, the command shows a notice that online help is not configured and no window appears. Release 1.2.1 and earlier named the setting gem_url; updating with the installer renames it to assistant_url and replaces an old assistant address with the current one. The explanation window is the same one the Help menu item shows, and both take the same route through the Logic process; ticking "Do not show this again" and selecting Assistant sets explain_before_open to false under [ai.help], which turns the window off for the command and the menu item alike. If Windows cannot start a browser, the notice "Wheelhouse could not open your browser." appears |
| patterns | Opens the Pattern Manager. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | "pattern manager", "what can I say", "show commands", "show all commands", and "show command list" also open it; see "Selected Commands in Detail" in the Voice Commands section |
| learn my voice | Opens the voice-teaching window, where Wheelhouse learns how you sound so it stops missing short words | "calibrate my voice" also works; only the Distil-Whisper speech engine uses it; See "Teaching Wheelhouse your voice" in the Speech Engines section |
| x-ray fix | Sends the selected text to the configured AI server for formatting correction -- capitals, numbers, amounts of money, common abbreviations, and obvious punctuation -- then replaces the selection with the result. The AI is told not to reword the text or add or remove words | Requires the AI server to be configured and reachable; Wheelhouse shows its progress and outcome on screen rather than out loud, and always preserves your original text on any failure |
| simplify | Rewrites the selected text in plain language, using shorter sentences and simpler words. Keeps every fact and leaves the layout alone -- line breaks, indentation, bullet marks, numbering, code lines and addresses come back unchanged. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Same AI server and same safeguards as "x-ray fix"; the selection comes back as plain text, so formatting applied in a word processor is lost |
| shorten | Rewrites the selected text more briefly, cutting repetition and padding. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Same AI server and same safeguards as "x-ray fix" |
| x-ray make formal | Rewrites the selected text in a formal register, avoiding contractions and casual wording | Same AI server and same safeguards as "x-ray fix" |
| pirate | Rewrites the selected text the way a pirate would say it. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | Ships as a worked example: it is the same action as the three above with a different sentence in the pattern file. See "Selected Commands in Detail" in the Voice Commands section for writing your own. |
| x-ray translate to [language] | Translates the selected text into the language you name, for example "x-ray translate to spanish" or "x-ray translate to brazilian portuguese". Keeps every fact and leaves names and numbers as they are. | Same AI server and same safeguards as "x-ray fix"; say the language in English and in lower case, as one or more plain words with no punctuation. How good the translation is depends on the model you have configured. |
| cancel fix | Cancels an in-progress fix or rewrite. Needs no prefix word. When no AI request is running, Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows the notice "No AI job is running." and types nothing. |  |
| boost | Adds the selected text to the speech recognition hints. Applies only when the word is the whole utterance; inside a longer sentence it dictates normally. | See "Selected Commands in Detail" in the Voice Commands section -- works only when the running engine applies hints: Google always, Parakeet and Distil-Whisper only with hint biasing on. Otherwise Wheelhouse types the word as dictation |

These commands control Wheelhouse itself: listening modes, help, personal patterns, and the AI features. "push to talk mode" and "click to talk mode" switch between the two listening modes. "x-ray fix" is described in the table above. It shows its progress on screen rather than out loud ("Correcting..."), then a notice with the outcome (for example "Done.", "No changes needed." or "Cancelled."), and leaves the original text in place if the request fails. Five further commands rewrite the selection instead: "simplify", "shorten", "x-ray make formal", "pirate", and "x-ray translate to [language]"; see "Rewriting the selected text" under [Selected Commands in Detail](#selected-commands-in-detail), which also covers adding others. "boost" adds the selected text to the speech recognition hints when the running engine applies hints (see "boost" below), "patterns" opens the Pattern Manager, "help" opens the Wheelhouse Assistant in the default browser after a short explanation window, and "cancel fix" stops a correction or rewrite still running; it needs no safety word either. Every other command in this paragraph written without the "x-ray" prefix needs no safety word, and each of those fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed.

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

This opens the **Pattern Manager**, which lists every voice command and text replacement, grouped by category. Selecting an entry shows its trigger phrase, its action, and whether it requires the safety word. Tick "Only my patterns" (Alt+M) to list only the patterns you added, duplicated, or customized; it works together with the filter box.

The Pattern Manager can **view** any pattern, including every built-in one; **create** personal patterns, such as a shortcut that types an email address, a correction for a misheard word, or a command that opens a program; **edit** and **delete** user-created patterns; **customize** a built-in pattern, where a personal copy with the same trigger overrides it and deleting that copy restores the shipped behavior; and **change the safety word** from "x-ray" to another word. To write your own expression or add several actions (advanced mode), click Add Pattern and choose **Create an advanced command**, or tick the **Advanced** check box in the editor. A new pattern of yours runs before the built-in patterns. A built-in pattern that you edit keeps the built-in's place in the order. Wheelhouse tries the patterns in order, and the first match wins, so a new pattern of yours beats a built-in pattern that matches the same words, and an edited built-in runs only when no pattern ahead of it matches.

Personal patterns live in a separate per-machine file, preserved across upgrades; the shipped patterns file is never modified. That file is `user_patterns.toml`, in the folder `%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\data`. It does not exist until you first save a pattern or change the safety word, and the Pattern Manager is the tool that edits it.

**Running a program from a pattern.** Two actions run programs. **Run a program** starts a program or a command line and does not wait for it to finish. **Run a program and capture its text** runs one program without a shell, waits for it to finish, and keeps the text it printed for the steps after it. A Python script runs the same way, through the Python interpreter. For example, a pattern that inserts what `C:\scripts\example.py` prints needs two steps in advanced mode. The first step uses **Run a program and capture its text**, with `python` in its program field and `C:\scripts\example.py` in an argument field that **Add argument** creates. The second step uses **Insert text**, with `run_capture` as its text: that is the name of the first step's action, and it stands for the text the script printed, which the step inserts at the cursor. The capture waits for the number of seconds in its timeout field, or for 10 seconds when that field is empty, and never for longer than 60 seconds. The 10-second default is the setting run_capture_timeout_default_s under [actions]. The printed text can hold at most output_cap_chars characters, a setting under [actions] that is 10000 by default. Longer output is not cut short: the step fails instead. The step also fails when the program is still running at the end of the wait, which stops the program, and when the program exits with an error code. A failed step stops the pattern, so nothing is inserted, and Wheelhouse types the spoken words as ordinary dictation instead. Every action and its parameters are listed in the [Action Reference](https://wheelhouse-project.org/reference.html#action-reference).

**Rewriting the selected text**

With text selected, **"simplify"** sends it to the AI server to be rewritten in plain language and replaces the selection with the result. **"shorten"** removes repetition and padding, **"x-ray make formal"** removes contractions and casual wording, and **"pirate"** rewrites the text in pirate speech. **"x-ray translate to [language]"** translates the selection into the language named -- "x-ray translate to spanish", "x-ray translate to brazilian portuguese"; multi-word names are recognised. All five use the same AI server as "x-ray fix" and leave the original in place if the request fails; they show "Rewriting..." where "x-ray fix" shows "Correcting...", then an outcome notice.

A rewrite pastes back plain text, so word-processor character formatting -- bold, italics, a font, a colour -- is lost on the rewritten part. Layout is preserved: line breaks, blank lines, indentation, bullets, numbering, and non-sentence lines such as a code line or a postal address return unchanged.

**Adding a rewrite command.** The five commands are one action carrying a different instruction each, so more can be added from the Pattern Manager without changing program code: create a pattern, choose the **Rewrite text with AI** action, and set its parameter to a sentence telling the AI the style required.

For example, to rewrite text at a reading level a younger reader can follow, create a pattern with the trigger **reading level** and this instruction:

> Rewrite this text so a ten-year-old could read it. Use short sentences and everyday words, and explain any term a ten-year-old would not know. Keep every fact. Return only the rewritten text.

Selecting a paragraph and saying "x-ray reading level" then rewrites it. Two constraints on that instruction: **describe the style only** -- Wheelhouse itself supplies the wording that preserves layout and keeps instructions inside the selected text from redirecting the AI, and duplicating either degrades the result -- and **end with "Return only the rewritten text."**, without which the AI is liable to prefix a sentence of commentary that is then pasted into the document.

**"cancel fix"** stops a request that is still running -- a correction or any of the rewrites -- and nothing is pasted. It needs no safety word. When no request is running, Wheelhouse types the words as ordinary text; if you said the safety word first, it shows the notice "No AI job is running." and types nothing.

**"help"**

Opens the Wheelhouse Assistant, the project's online help, in the default browser, where questions can be asked in plain language. The address is the assistant_url setting in [ai.help], which points at the assistant by default. Release 1.2.1 and earlier named the setting gem_url; updating with the installer renames it to assistant_url and replaces an old assistant address with the current one. [Getting Help](#getting-help) gives the notice that appears when the address is missing.

A short explanation window comes first, the same one the **Help** menu item shows: it explains that the Wheelhouse Assistant runs on Google's Gemini Notebook, so you must sign in with a Google Account, and its **Assistant** button opens the browser. Ticking **Do not show this again** and selecting **Assistant** sets explain_before_open to false under [ai.help], after which "help" opens the browser directly. When the window appears again after **Cancel**, and how to bring it back, are in [Getting Help](#getting-help).

## Speech Modes

Wheelhouse has no command mode and no dictation mode to switch between. Each utterance is classified as it arrives, using the position of the words within the phrase.

### Classification of spoken input

- **Command**: the utterance matches a voice command and the command is performed. "undo" presses the undo shortcut; "delete five" deletes five characters. "erase five" does the same, because "erase" is a synonym for "delete". Nothing is typed.
- **Dictation**: the utterance is typed into the focused text field. "dear Sarah thank you for the update" is typed as those words.
- **Inline replacement**: certain words are replaced with symbols or corrected spellings within dictation. "hello comma world" produces "hello, world"; the word "comma" is replaced by the punctuation mark rather than typed.

### Word position determines classification

- **The first word of a phrase is a candidate command.** When speech starts after a pause, the first word is checked against the set of known command openings. If it could begin a command, it is held briefly -- well under a second -- to see whether the following word or two completes one. If you are still speaking when that time ends, the hold continues until the next word arrives or the phrase ends, for at most five more seconds with the shipped settings. "delete five" spoken as its own phrase runs the command. If the words match no command, they are typed as ordinary text; no words are discarded.
- **Words in the middle of a phrase are dictation.** "I want to delete five items" is typed in full, including the word "delete", because "delete" did not begin the phrase.
- **Replacement words apply in any position.** Words such as "comma" and "period" are replaced whether they occur first, last, or mid-sentence, since their purpose is to appear within dictation.

### Commands that need the safety word

The safety word protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands run only when the utterance begins with "x-ray", as in "x-ray close window". Other commands do not require it. The safety word follows the same position rule: "x-ray" carries its special meaning only as the first word of a phrase, and is typed as text anywhere else. If "x-ray" is followed without a pause by words that are not a command, the whole phrase including "x-ray" is typed as text. If you pause after "x-ray" for longer than the command wait (700 ms with the shipped settings), only the words spoken after the pause are typed.

### Streaming insertion

Recognized words are inserted while speech continues, rather than after the utterance ends. Two exceptions are brief holds, normally fractions of a second: one at the start of a phrase while a command match is evaluated, which can last longer while you are still speaking, as described above, and a similar one around replacement words. A third exception is not a hold. While Wheelhouse reads the window -- to put numbers on the controls, or to find a control named in a "click" command -- a word spoken in that moment is dropped instead of typed, and it is not typed afterwards. Wheelhouse drops it deliberately: a word held until the read finished would be typed seconds later, into whatever had focus by then. The refusal lasts no longer than the read itself, and it cannot continue without limit.

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
- **STT Provider** -- select the speech engine. Only engines set up on this computer are listed, plus **Parakeet (model not installed)** when the installer turned Parakeet off because its speech model is missing. See [Speech Engines](#speech-engines). The last item in this list, **Teach WheelHouse your voice...**, opens the voice-teaching session for the Distil-Whisper engine. See [Teaching Wheelhouse your voice](#teaching-wheelhouse-your-voice).
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
- The Sonos and Sony Bravia plugin sections, headed `[plugins.sonos]` and `[plugins.bravia]`, are off by default; their comments say to turn them on only if you own that hardware.

**The per-setting reference** -- every user-facing key that the shipped config.toml contains, its default, and what it does -- is in the [configuration reference](https://wheelhouse-project.org/reference.html). The three optional Sonos settings that the file leaves commented out are described under [Plugins](#plugins). The file also leaves PATTERN_MANAGER_FONT_SIZE commented out: the font size, in points, of the Pattern Manager window. In that window, Ctrl+= makes the text larger, Ctrl+- makes it smaller, and Ctrl+0 returns it to the default size, and each change is saved to this setting. The allowed range is 7 to 24; a value outside it is moved to the nearest end. The value must be a whole number written without a decimal point: 12.0 is ignored. Two settings are worth knowing before opening the reference. Transcript logging (LOG_TRANSCRIPTS) is off by default, which keeps dictated words and clipboard contents out of the log files; turn it on only while diagnosing a recognition problem, then turn it back off. The AI server's API key is never stored in config.toml: if your server requires one, set the WHEELHOUSE_AI_API_KEY environment variable instead, keeping the key out of a file that could be copied or shared.

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

Volume control through this plugin requires a specific arrangement: a Sonos sound bar connected to the display this computer uses, receiving the computer's or the display's audio. Wheelhouse sends volume commands to the Sonos only when this plugin is enabled and two conditions hold at once -- Windows is playing to an external audio device rather than the machine's own speakers, and the Sonos reports that it is receiving television audio. In any other arrangement, including Sonos speakers elsewhere in the house, volume commands go to the normal Windows volume instead. Settings:

- `polling_interval` -- how often, in seconds, to check whether music is playing (default 2).
- `speaker_ip` -- optional. Automatic discovery runs once at startup, and only when this plugin is enabled and the Windows output device is an external one: Wheelhouse reads the name of the default output device and skips discovery when that name looks like built-in hardware (Realtek, Intel, "Speakers", "Headphones", and similar). A discovered speaker wins over this setting; set it when discovery finds nothing.
- `request_connect_timeout` / `request_read_timeout` -- advanced network timeouts (defaults 2.0 and 5.0 seconds); rarely need changing.

It connects to the speaker over your home network directly -- no Sonos account or internet service is involved.

The pause this plugin applies is narrow: it fires only when the Sonos is playing from a music service, and not when the Sonos is playing audio it received from this computer or from the television. That is deliberate, so that watching a film does not stop Wheelhouse from listening.

A second, separate mechanism can pause listening for computer audio, whether or not you own a Sonos. It is set by `ENABLE_AUDIO_SUPPRESSION`, which takes three values. With `"auto"`, the default, Wheelhouse asks Windows once at startup whether the default microphone has an echo canceller. When Windows reports one, sound this computer plays does not pause listening. When Windows reports none, or cannot answer, Wheelhouse watches the sound level of the Windows output device and pauses recognition while sound is playing through it. On a machine where Windows reports no echo canceller, sound from a screen reader also pauses listening. `true` pauses listening whenever sound plays, whatever Windows reports, and `false` never pauses. A settings file from an older install has `true`; change it to `"auto"` to let Windows decide. A microphone plugged in or made the default after startup is checked at the next start. While the pause applies, the check runs on an interval that widens to ten seconds while audio is playing, and listening resumes by itself only after the sound has stayed quiet for about three seconds, so listening can take up to about thirteen seconds to resume after the audio stops. In click-to-talk mode, saying the wake word ("computer") ends the pause at once, while the sound still plays. So audio your computer plays can pause listening -- including audio it plays through a Sonos, when the Sonos is the Windows output device. What the Windows output device never sees, and therefore never pauses listening for, is audio that reaches the Sonos without passing through this computer: television audio over HDMI or an optical cable, and music the Sonos streams by itself.

### System Volume

Controls the normal Windows volume (the same one as the taskbar speaker icon) from the volume scroll zone, and quiets system audio while you hold the push-to-talk button. Enable with `plugins.system_volume.enabled` (default: enabled). Settings:

- `device_type` -- which audio device to control: `"default"` (the usual choice) or `"communications"`.
- `volume_step_db` -- loudness change per wheel step, in decibels (default 1.5).
- `min_volume_db` / `max_volume_db` -- the volume floor and ceiling (defaults -96.0 and 0.0).

Fully local, no network. Both volume plugins can stay enabled: at startup Wheelhouse picks one to receive volume commands -- Sonos when the Sonos plugin is enabled, the Windows output device is external, and a discovered Sonos reports that it is receiving television audio; System Volume in every other case. With `plugins.sonos.enabled` set to false, volume commands always go to System Volume, and Wheelhouse does not search the network for Sonos speakers.

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
- If the thumb wheel does nothing, check the screen zones: pointer at the left edge of the screen adjusts brightness, anywhere else adjusts volume -- and at least one plugin for that control type must be enabled.

## Troubleshooting

Start with the verification checks below. The Wheelhouse Assistant can also read an error message or a log excerpt and identify the cause; see [Getting Help](#getting-help).

### Setup verification

Run these five checks in order. Stop at the first that fails and read the entry it names.

1. **Did the installer finish without error lines?** If not, see [Installer troubleshooting](wheelhouse_install.md#installer-troubleshooting) in the installation guide.
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
- *Action:* Run the Wheelhouse installer again and select Yes when Windows asks for permission. The installer then installs the runtime for you. It also runs its other steps again: it replaces Wheelhouse with the release that installer provides, which can be newer than the one you have, and keeps your settings and voice patterns. The wizard also pre-ticks "Start Wheelhouse automatically when I log in" and "Start Wheelhouse now, when setup finishes", so clear a box you do not want. If you already did that, restart the computer: Microsoft's installer sometimes needs a restart before Windows uses the new runtime. If you cannot use that route -- for example your account cannot approve the Windows permission prompt -- ask someone with an administrator account to install the Microsoft Visual C++ Redistributable (x64) from Microsoft's own page at https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist, under the heading "Visual C++ v14 Redistributable". Start Wheelhouse again afterward.

**Commands not recognized**

- *Symptom:* A command such as "maximize" has no effect, or is typed as text.
- *Likely cause:* The speech engine returned a different word, for example "maximum" instead of "maximize", or the utterance overlapped with playing audio.
- *Action:* Speak the command as a separate utterance, with a brief pause before it, at normal conversational volume; raised volume reduces accuracy. If one word is misheard repeatedly, select a correctly spelled copy and say "boost" -- see its entry in [Voice Commands](#voice-commands). Only Google applies hints as shipped; Parakeet and Distil-Whisper apply them only with hint biasing on. Otherwise "boost" is typed as a word.

**Command words are typed as text instead of running**

- *Symptom:* "close window" is typed into the document instead of closing the window.
- *Likely cause:* Expected behavior. The safety word protects commands that would be disruptive or hard to undo if they fired while you were dictating. These commands require "x-ray" first.
- *Action:* Say "x-ray close window". The Voice Commands section of this document marks which commands require the safety word.
- *Note:* The reverse also happens. A command that needs no safety word, such as "click [name]" or "activate [app name]", is typed as text when Wheelhouse finds no control, window, or program with that name. Saying "x-ray" before it shows a notice that explains the failure and types nothing.

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

---

## Getting Help

**The Wheelhouse Assistant is the first place to ask.** It holds this entire document, the reference for actions, notices, and configuration settings, and the project's own notes on how each part behaves. It answers questions in plain language, reads an error message or a log excerpt and identifies the likely cause, and names the specific setting to change and the file it belongs in. It covers material this document summarizes, so it can answer questions no section here addresses.

<https://notebook.google.com/notebook/da51a404-67ec-4804-9ebe-83605df3e9cf/preview>

Three ways to reach it: the address above, the **Help** item in the right-click menu on the floating button or the tray icon, and the spoken command "help". All three open the same assistant in the default browser.

The Wheelhouse Assistant runs on Google's Gemini Notebook, so you must sign in with a Google Account. The account is free, and Google does not ask for a credit card. You can use an email address you already have; a Gmail address is not necessary. To create an account, click "Create account" on the Google sign-in page. If you are signed in with a work or school account and the Assistant does not open, sign in with a personal account instead.

The menu item and the spoken command show a short explanation window first, headed **Ask the Wheelhouse Assistant**. It says what the assistant is and repeats the Google Account text above. Its **Assistant** button opens the assistant; **Cancel**, or the Escape key, closes the window and opens nothing. Tick **Do not show this again** and then select **Assistant**, and Help opens the browser directly from then on. While `explain_before_open` is false and the file help_explainer_notebook_shown.toml is missing from the data folder next to the settings file, the window appears at every Help with the box already ticked. Only the **Assistant** button writes that file, so after **Cancel** the window appears again at the next Help; select **Assistant** with the box still ticked to keep the window off. If Windows cannot start a browser, Wheelhouse shows the notice "Wheelhouse could not open your browser." If the notice says "Online help is not configured. Set assistant_url under [ai.help].", the settings file holds no assistant address; set assistant_url under [ai.help], then restart Wheelhouse. Release 1.2.1 and earlier named the setting gem_url; updating with the installer renames it to assistant_url and replaces an old assistant address with the current one. To bring the window back, set `explain_before_open = true` under `[ai.help]` in the settings file; see [Configuration](#configuration).

The assistant does not have access to a particular computer, so it cannot read logs that are not pasted into it, and it does not know about changes made after the release it was built from.

**Reporting a defect.** A problem in Wheelhouse itself -- something that behaves incorrectly rather than something that needs explaining -- goes to the project:

- Open an issue or start a discussion at https://github.com/wheelhouse-project/Wheelhouse.
- Or email `help@wheelhouse-project.org`.

Include the Wheelhouse version from **About Wheelhouse** in the right-click menu, what was done, what was expected, and what happened instead. Paste any error message in full, and attach the installer's setup log if the installer failed. Installer messages and log lines contain no dictated text unless transcript logging was switched on; see [Configuration](#configuration).

---

Generated: 2026-10-01 for the v1.2.2 release
Wheelhouse version: 1.2.2
