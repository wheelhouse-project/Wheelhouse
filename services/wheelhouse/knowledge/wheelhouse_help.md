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

Wheelhouse controls a Windows PC by voice. It has five functions. It types dictated text into any application, runs spoken commands, switches windows, starts programs, and clicks on-screen controls by name. None of them require the keyboard or mouse. On a Logitech MX-series mouse, Wheelhouse can also assign screen brightness and volume to the thumb wheel.

**Intended use.** Wheelhouse is a general-purpose voice interface and an assistive technology. A user for whom a keyboard and mouse are painful, difficult or impossible can operate the computer through the voice interface alone.

**Requirements.**

- A Windows 10 or Windows 11 PC (64-bit)
- A microphone. The built-in microphone of a laptop is usually sufficient. If recognition accuracy is poor, try a headset or an external microphone. You can connect one after you install Wheelhouse.
- About 10 GB of free disk space. The speech model uses 2.5 GB of that space. The program and the Python environments for its speech engines use the rest.

The installer provides everything else and checks the hardware before it begins. You do not need an account, a subscription, or other software on the PC before you install. The full requirements, with memory and processor, are in [System Requirements](#system-requirements) below.

**Where Wheelhouse processes speech.** By default, Wheelhouse converts speech to text on the local machine. It does not transmit any audio or text, and the application reports no telemetry.

Cloud speech recognition is available. It is not the default. You can select it at install time or afterwards. [Speech Engines](#speech-engines) compares the engines and states what each one transmits.

**Update check.** Once a day, Wheelhouse asks api.github.com if a newer public release exists. Wheelhouse sends the first request at least 60 seconds after it starts. The request carries no user data, but GitHub sees the IP address. When a newer release exists, a notice names the new version and offers to install it.

To turn the automatic update check off, set `CHECK_FOR_UPDATES = false` in config.toml. You can also check at any time. Choose **Check for updates** in the About Wheelhouse window, or say "check for updates". A manual check sends the same one request as the automatic check, also when `CHECK_FOR_UPDATES` is false. It stores nothing.

**Operation.** Wheelhouse converts microphone audio to text and then classifies the result. The default engine, Parakeet, converts the audio on the local machine. The Google Cloud engine sends the audio to Google's servers, which convert it. 

When text matches a known command ("undo", "select all"), Wheelhouse runs that command. All other text is dictation. Wheelhouse inserts dictation into the focused window, for example a document, an email, or a chat field, and applies capitalization and spacing automatically. You speak punctuation. "comma" and "period" insert the corresponding symbols.

Wheelhouse inserts text continuously while you speak. The text typically begins to appear within two seconds. Wheelhouse does not wait until the utterance ends. [Speech Modes](#speech-modes) explains how Wheelhouse decides the classification.

**How to get answers.** The Wheelhouse Assistant answers questions about any part of this document in plain language. You do not need to find the right section first. It also holds the reference for actions, notices, and configuration settings, so it answers questions this document does not cover. See [Getting Help](#getting-help).

**Project status.** Wheelhouse is an open-source project with a single primary author. The author uses it daily, but testing has used only a limited set of machines, so expect defects on untested hardware and in untested applications. Report failures at https://github.com/wheelhouse-project/Wheelhouse

---

## Quick Start

Complete these steps before you read the rest of this document. They show that the installation works.

1. Download the installer and run it: https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/Wheelhouse-Setup.exe
   If Windows shows "Windows protected your PC", click "More info". Check that the publisher reads David Chesley Hite III. Then click "Run anyway". The pre-selected answers in the setup wizard are right for almost everyone. The wizard downloads the speech model, so allow 10 to 20 minutes for it.
2. Start Wheelhouse from the Start menu or the desktop shortcut (the installer creates both).
3. Open Notepad.
4. Say **"hello world"** -- the words "hello world" appear.
5. Say **"new line"** -- the cursor moves to a new line.
6. Say **"undo"** -- the new line from step 5 disappears.
7. Say **"select all"** -- all of the text becomes highlighted.

If steps 4 to 7 produce the results described, the installation works.

Wheelhouse starts in click-to-talk (toggle) mode and starts to listen, which is why steps 4 to 7 need no click. One click on the floating button or the tray icon turns listening off. Another click turns it on again. To hold a button down while you speak instead, see [Interaction Modes](#interaction-modes) below.

If a step does not behave as described, see [Setup verification](#setup-verification) in Troubleshooting. You can also ask the Wheelhouse Assistant, which answers questions about any part of this document ([Getting Help](#getting-help)).

---

## Where to Go Next

Once the quick start works, continue with the section that matches the task.

- **To dictate text into email, documents and chat:** [Voice Commands](#voice-commands), in particular the dictation and punctuation subsections, then [Speech Modes](#speech-modes).
- **To use the full command set:** the [Voice Commands](#voice-commands) section lists every shipped command and covers formatting and navigation. Then read [Configuration](#configuration). The [configuration reference](https://wheelhouse-project.org/reference.html) lists every user-facing setting in config.toml.
- **To install, configure or diagnose a fault:** [Installation and Setup](wheelhouse_install.md#installation-and-setup), then [Configuration](#configuration), then [Troubleshooting](#troubleshooting). If none of those sections answers a question directly, the Wheelhouse Assistant answers from this document in plain language. See [Getting Help](#getting-help).

---

## Speech Engines

The installation guide describes the speech engines. It covers the account each one needs, how the engines compare, and how to add or switch engines. See [Speech Engines](wheelhouse_install.md#speech-engines) in the installation guide.

### Your training word

The Parakeet engine can miss the first sound of the first word after a pause. A short recording of you saying "hello", which this guide calls the training word, helps it keep that sound. When a training word is on, the engine puts the recording in front of what you say each time it decodes. The engine removes the recording's own word from the result, so Wheelhouse types nothing extra. Only the Parakeet engine uses the training word. If a different engine is active, the **Your training word** window says so and offers no **Record** button.

Nothing asks you to record a training word, and no recording ships with Wheelhouse. The training word stays off until you keep a recording.

Open the **Your training word** window in any of three ways:

- Right-click the floating button or the tray icon, open **Speech-to-Text Provider**, and choose **Your training word...**.
- Open the Settings window and select **Training word...** in the **Other settings** group.
- Say "record my training word". The command works only as a whole utterance. Inside a longer sentence, Wheelhouse types the phrase as text.

Only one window opens. When the window is already open, each way brings it to the front. The window shows "No recording" or "Recording from" the date and the microphone name. The window also shows if the training word is on or off. If the microphone in use now differs from the microphone that made the recording, the window suggests a new recording. The training word stays on.

**Record** works while speech is on. In push-to-talk mode it also works while speech is off between holds. After you select it, the window asks you to say "hello" once, at your normal speaking volume, and then to stay quiet. In push-to-talk mode, hold the floating button while you say it. The take ends when you release the button. One press of **Record** makes one take.

Wheelhouse waits up to 10 seconds for the word. The take ends about one second after you stop, or four seconds after you start, whichever comes first. While the take runs, Wheelhouse types nothing and runs no command. If you close the window during a take, the take ends and Wheelhouse keeps nothing.

Wheelhouse checks each take before it offers it to you. Wheelhouse does not use a take that fails a check. The window shows the reason with a **Record again** button:

- "No speech heard".
- "Too quiet".
- "The recording is too loud and clips".
- "The word is too long" (more than one second of voice).
- "The word is too short" (less than a quarter of a second of voice).
- "You started too soon after the last sound".
- "More than one word heard".
- "No word was recognized" or "The word was not hello", when the engine does not hear "hello" alone in the take.

Background noise does not refuse a take. Wheelhouse measures the noise outside the word and cuts the word so that the noise on each side of it stays out. When the word is less than 30 dB above the background noise, a take that passes also shows "The background noise is high. Wheelhouse works better in a quieter place or with a better microphone." The limit is `lead_word_noise_advice_db` in `[engine]` of the Parakeet engine's own `config.toml`. An update of Wheelhouse sets it back to 30.

A take that passes shows its length in milliseconds, its level, and "One word: hello", with these buttons:

- **Play new recording** plays the take.
- **Keep new recording** saves it as the training word and enables the training word at once, with no restart. The button always reads "Keep new recording".
- **Discard** deletes the take.

While a new recording waits, these are the two choices: **Keep new recording** and **Discard**. The window shows no **Close** button. If you close the window with the X in the title bar or with the Escape key, Wheelhouse deletes the new recording. **Record** and **Discard** never change the saved recording.

A line above the buttons says what **Keep new recording** replaces. It reads "Keep new recording replaces the recording from" and the date of the saved recording. It reads "Keep new recording replaces the word file" and the whole file path when the engine's settings already name a different file. The line wraps, so a long path stays readable. Wheelhouse hides the line when no recording exists and the setting is empty.

At the first keep, Wheelhouse copies a file that the settings name to the previous recording and never changes that file itself. When that copy becomes the saved recording again, the window reads "Recording from your earlier file", and the line above the buttons reads "Keep new recording replaces the recording from your earlier file."

**Play current recording** plays the saved recording. The window shows the button disabled when no recording exists. Playback uses the Windows default output device. Wheelhouse disables transcription for the length of the sound plus 0.3 seconds, so Wheelhouse does not type the sound. Then Wheelhouse returns transcription to its earlier state. You can play each recording as many times as you want.

**Go back to previous recording** appears while an older recording exists. The window hides it while a new take waits for **Keep new recording**. It swaps the saved recording with the previous one and enables the training word. A second press returns to the recording that the first press replaced.

**Turn off** disables the training word and keeps the recordings. The window shows this button disabled when no recording exists. After you select it, the same button reads **Turn on**. When you select **Turn on**, the saved recording is on again.

Every button works with a spoken click. Say "click keep new recording", or "show numbers" and then "click" with a number.

The recordings are in the folder `%LOCALAPPDATA%\WheelHouse\lead-word\`. `hello.wav` is the saved recording. `hello.previous.wav` is one older copy. `hello.candidate.wav` is a take that passed and waits for **Keep new recording**. `hello.json` holds the date, the microphone name, the measurements, and the word the engine heard. The files stay on the computer. Nothing sends them anywhere.

When you keep a recording, Wheelhouse sets `lead_word_path` in `[engine]` of the Parakeet engine's own `config.toml` to the path of `hello.wav`. **Turn off** empties the setting. An update of Wheelhouse keeps the value.

---

## System Requirements

This section lists the hardware that Wheelhouse requires and the hardware on which it performs well. It also gives the response times to expect between the two.

### Minimum requirements

- Windows 10 or Windows 11, 64-bit
- A dual-core processor. Wheelhouse installs and runs on it, but speech recognition can respond slowly. A processor with 4 or more cores is the comfortable minimum
- 8 GB of RAM for the offline speech engine, Parakeet. With less memory, the installer stops for that engine but still installs the Google Cloud engine. In that case, choose Google Cloud in the setup wizard. The one-line command-line installer asks one question when you choose no engine.
- 6 GB of RAM for any speech engine, and the cloud engine needs it too. This is a hard minimum. With less memory, the installer stops for every engine. The only fix is to add memory.
- 10 GB of free disk space
- The project strongly recommends an SSD. On an old hard disk drive, startup and first responses are noticeably slower.
- A working microphone

### Recommended requirements

- 16 GB of RAM
- A modern quad-core or better processor (roughly, anything sold in the last six or seven years)
- A microphone that is close to the speaker. A laptop's built-in microphone is usually sufficient. If recognition accuracy is poor, try a headset or an external microphone before you change any settings. Microphone placement and background noise affect accuracy more than most configuration values do.

### Estimating performance

- A computer that runs a browser with several tabs and a video call at the same time without trouble is sufficient for Wheelhouse. That is the practical baseline.
- A computer that already responds slowly to basic tasks, for example when you switch windows or type in a browser, shows the same delays in Wheelhouse. It still works. Responses take longer.
- Installation costs nothing. You can run it again, and you can reverse it. For these reasons, a measurement on the machine itself settles the question. If dictated words regularly take 3 to 4 seconds or more to appear, change engines. See [Speech Engines](#speech-engines).

### What speed to expect

- **Modern hardware, default engine:** roughly 1.5 to 2 seconds pass from the time you speak to the time the first word appears. After that, words continue to arrive while you speak, not at the end of the sentence.
- **Older or slower processors:** 3 to 5 seconds or more to the first word is possible. A cloud engine usually responds faster on such machines, because the recognition work runs elsewhere.

### Improving performance on slower hardware

- When you dictate, close programs that need much processor power. Browsers with many tabs, video editors and games compete for the processor that the speech engine uses.
- Consider the Google Cloud engine. Almost no recognition work runs on the local machine, so a slower computer still gets fast, accurate results. The trade-offs are the account setup, the privacy difference, and the requirement for an internet connection. See [Speech Engines](#speech-engines).
- Disable unused features. If you have no Sonos speakers and no Sony Bravia TV, leave those plugins disabled, so nothing extra runs in the background. The plugins are off by default. See [Plugins](#plugins).
- Slower machines sometimes run a command before the speaker finishes the phrase. You can raise the speech timing values in the settings file to compensate. See [Settings for slower hardware](#settings-for-slower-hardware) in Configuration.

If none of these produces acceptable response times, the Wheelhouse Assistant can help you find which setting to change for a specific machine. See [Getting Help](#getting-help).

## Voice Commands

Wheelhouse converts speech into keystrokes, text, and system actions. Most commands require no prefix. The safety word protects commands that would be disruptive or hard to undo if they ran while you dictated. These commands require **"x-ray"** first. Most rows write the "x-ray" prefix in the first column, but a few leave it out. The description of each of those rows says "Needs the safety word first". Those commands also require "x-ray".

A few commands that begin with a common word run without the safety word: "click [name]", "activate [app name]", "switch to [app name]", "show [app name]", "minimize [app name]", "maximize [app name]", and "cancel fix". If you say "x-ray" before one of them, it still runs.

A command of this kind fails when no control or window has that name, no program starts, Windows does not bring the window forward, or no AI request is active. If such a command fails and you did not say the safety word, Wheelhouse types the words you said as ordinary text and shows no notice. If it fails and you did say the safety word, Wheelhouse shows a notice that explains the failure and types nothing.

Two cases differ. In the first case, "right click [name]" and "double click [name]" show the notice and type nothing even without the safety word. The second case is a command that names an application ("activate", "switch to", "show", "minimize", "maximize", "x-ray go to", or "x-ray close"). If such a command starts a program and the window does not come forward in time, or the command gets no answer in time, Wheelhouse stops without typing anything or showing a notice. It does this with or without the safety word.

There are two kinds of voice pattern. **Commands** do an action, for example press a key, switch a window, or click a button. You normally speak a command as a complete utterance. Say the command, then pause.

**Replacements** apply inline during dictation. You speak a replacement in the middle of a sentence, and Wheelhouse replaces the recognized word with a symbol or corrected text as it types the text. All punctuation words ("period", "comma", "question mark") are replacements, so you do not have to stop dictation to insert punctuation.

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

**Example 1: Dictate and correct an email**

1. Dictate the body of the message and speak the punctuation inline: "hi team comma new paragraph the release is ready period"
2. To correct a misheard word, say **"delete last word"** to remove it, then dictate the word again.
3. To correct the capitals, numbers, and punctuation of a paragraph, select it with **"select paragraph"**. Then say **"x-ray fix"**. Wheelhouse sends the paragraph to the AI server and replaces it with the corrected version.
4. Say **"activate outlook"** (substitute your mail application) to bring its window forward. If the application is not open, Wheelhouse starts it. Then say **"submit"** to press Enter.

**Example 2: Search for copied text**

1. Select a phrase with the mouse, or say "select word".
2. Say **"copy"**.
3. Say **"browser"**, as the whole utterance, to bring the default browser forward.
4. Say **"paste"** while the address bar has focus. Then say **"submit"** to press Enter.

### Full Voice Command Reference

Each table below lists the voice commands and replacements of one group under the heading of that group, one row for each. The notes of a row name other wordings that run the same command. Two filters remove speech instead of acting on it. The text after the Dictation Control table describes them. The text after each table covers the behavior that a table cannot show. This text tells you how to dictate a word that is also a command, the key names that "press" accepts, and how the navigation, punctuation, and click commands work.

#### Dictation Control

| Say this | What happens | Notes |
|---|---|---|
| literal [words] | Types the words after "literal" exactly, skipping all command and replacement processing. Takes effect wherever it appears in an utterance, not only as the first word. | The way to type a word that is also a command -- see "Selected Commands in Detail" in the Voice Commands section |
| insert [text] | Inserts raw text with no capitalization, spacing, or formatting applied | Useful for exact fragments like an email address or a product code |
| no space [words] | Types the words that follow with every space removed | e.g. "no space hello world" types "helloworld" |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item one", "item five" |
| numeric twenty three | Types a spoken number as digits, like "23", with no period | e.g. "numeric five" types "5"; "numeric forty two" types "42". "numeral" is a synonym: "numeral seven" types "7" |
| submit | Presses Enter. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |
| submitted | Presses Enter. Fires only when it is the whole utterance. |  |
| type [words] / dictate [words] | Types the words that follow as ordinary dictation, so a phrase that would otherwise run as a command is written out instead | e.g. "type delete all" writes those words rather than clearing the field |
| enter | Presses the Enter key. Fires only when it is the whole utterance. | "submit" does the same |

These commands control the text that Wheelhouse types, and they let you dictate a word that collides with a command. "literal [words]" types the words that follow exactly and skips all command and replacement processing. "insert [text]" inserts raw text with no capitalization, spacing, or formatting.

"submit" presses Enter when it is the whole utterance. The command "numeric [number]" also works only as the whole utterance. It types the number as digits with no period: "numeric twenty three" types "23". "numeral" is a synonym of "numeric": "numeral seven" types "7".

Wheelhouse discards utterances that begin with "okay Google", "ok Google", or "hey Google", so it does not transcribe speech for a nearby voice assistant.

Wheelhouse removes the hesitation sounds "um", "uh", "hmm", "mhm", "mm-hmm", and "mm-mm" from dictation wherever they occur in an utterance, so it does not type them.

In dictation, the speech engine writes numbers as digits by one rule. The number words one to nine stay words: "buy seven apples" types "buy seven apples". A single number word from ten to ninety becomes digits: "buy ten apples" types "buy 10 apples", and "twelve" types "12". Longer numbers, such as "twenty one" or "fifteen thousand", also become digits.

To keep a single number word as spoken, start with "literal". For example, "literal ten" types "ten".

#### Text Editing

| Say this | What happens | Notes |
|---|---|---|
| backspace [number] | Deletes one character to the left, or that many with a number | e.g. "backspace five" or "backspace twenty three"; the number is optional, counts capped at 50. "back space" also works. |
| delete [number] | Deletes one character (or that many) to the right. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | e.g. "delete five" or "delete twenty three"; counts capped at 50. Say "erase" in place of "delete" here too, as in "erase five". |
| delete (or erase) word | Deletes the entire word under the cursor |  |
| undo [number] | Undoes the last action, or several. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Ctrl+Z; e.g. "undo three". "undo that" also works. Common mishearings "undue" and "undu" also fire |
| redo [number] | Redoes the last undone action, or several. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Ctrl+Y; "redo that" also works, and the common mishearing "redu" also fires |
| new line | Inserts a line break without submitting the field | Works inline during dictation |
| new paragraph | Inserts two line breaks | Works inline during dictation |
| tab [number] | Presses Tab that many times | e.g. "tab three" or "tab eleven"; "indent three" does the same. The number is required here; a bare "tab" spoken on its own presses Tab once, and "tab" inside a longer sentence is typed as the word |
| shift tab | Outdents (Shift+Tab) | "outdent" does the same |
| escape | Presses the Escape key, then hides the numbers and the mouse grid if either one is shown. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. When neither the numbers nor the grid is shown, it only presses the Escape key. | "dismiss" and "dismissed" also work. "dismiss the meeting invite" on its own is typed as ordinary text, not treated as this command. |
| press [keys] [number] times | Presses any key or key combination by name, once, or that many times with "[number] times" | e.g. "press enter", "press alt f four", "press f five", "press tab three times" or "press control z two times"; the count is optional, counts capped at 30. "press escape" also hides the numbers and the mouse grid, as "escape" does, and so does "press esc" or "press escape" with a count; a combination such as "press control escape" does not. See the press-keys detail subsection of the Voice Commands section |
| copy | Copies the current selection. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "copy that" also copies |
| copy line | Copies the entire current line |  |
| copy all | Copies everything in the current field |  |
| copy screen | Starts the Windows screenshot snipping tool |  |
| cut | Cuts the current selection. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "cut that" also works. Say it as the whole sentence; "cut the vegetables" on its own is typed as ordinary text, not treated as this command. |
| paste | Pastes the clipboard contents. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "paste that" and "paste here" also work |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires the safety word |
| select all | Selects everything in the current field |  |
| delete (or erase) all | Selects everything in the current field and deletes it | Say it as the whole sentence. "delete all the files" on its own is typed as ordinary text, not treated as this command. |
| select word | Selects the word under the cursor | "select this word" also works. "select word by word until it looks right" on its own is typed as ordinary text, not treated as this command. |
| select line | Selects the line under the cursor | "select this line" also works. "select line six and copy it" on its own is typed as ordinary text, not treated as this command. |
| select paragraph | Selects the paragraph under the cursor | "select this paragraph" also works. "select paragraph three of the contract" on its own is typed as ordinary text, not treated as this command. |
| x-ray select [words] | Selects the first place those words appear in the document. The words must match the document exactly, apart from capital letters. | Wheelhouse shows a Windows notification when it selects nothing, so a screen reader can read the reason. |
| save | Saves the current document (Ctrl+S). Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |
| x-ray find [text] | Opens the app's find bar and types the search term | e.g. "x-ray find invoice" |
| replace | Opens find-and-replace (Ctrl+H). Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |
| search | Copies the current selection and runs a web search for it. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Select the text first |
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

Wheelhouse accepts common mishearings of "undo" and "redo" ("undue", "undu", "redu"), so the command still fires on those spellings. Wheelhouse caps the deletion counts for "backspace" and "delete" at 50. "tab [number]" requires the number. A bare "tab" spoken on its own presses Tab once; "tab" inside a longer sentence is typed as the word.

Say "erase" in place of "delete" in any delete command. "erase word", "erase all" and "erase five" do the same as "delete word", "delete all" and "delete five". The word "backspace" has no such alternative.

Wherever a command takes **[number]**, say the count: "backspace fifteen", "delete twenty three". Wheelhouse reads number words up to "nine hundred ninety nine", and each command still applies its own limit afterwards.

A spoken count can have more than one word. A command with a count waits until no next word can make the number larger, and at the latest until the end of the sentence. "backspace twenty three" deletes twenty-three characters.

The wait depends on the number, not on how many words you used. "fifteen" and "ninety nine" are complete as spoken. "twenty" and "fifty" can still become "twenty three" or "fifty five", so those wait for the next word. Every count command works this way, and this includes "backspace". Cursor moves read a count in their own way. See the paragraph on counts in the Navigation part of this section.

##### The "press [keys]" Command in Detail

"press [keys]" sends any keyboard shortcut. Wheelhouse holds down the modifiers first, whatever the spoken order, so "press delete control" equals "press control delete". If Wheelhouse does not recognize a word in the phrase, it presses nothing and types the whole phrase, with the word "press", as dictation. Wheelhouse splits hyphenated tokens from the speech engine, such as "control-alt", automatically.

**Modifier keys**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: F1 through F12. Say the letter f and then the number: "press f five" presses F5.

**Letters**: any single letter a through z.

**Digit keys**: the number words zero through nine name the digit keys, so "press control two" presses Ctrl+2 and "press shift five" presses Shift+5. Right after "f", a number from one to twelve names a function key instead. "press f zero" presses F and 0.

**Repeat count**: end the phrase with "[number] times" to press the keys that many times: "press tab three times", "press control z two times", "press down arrow twenty times". Wheelhouse caps counts at 30, and it accepts "one time". Wheelhouse requires the word "times". Without it a trailing number is part of the keys, so "press control two" presses Ctrl+2 once and "press f five" presses F5 once.

**Symbols by spoken name**: the command presses these symbols correctly: backtick, semicolon, slash (forward slash), backslash (back slash), comma, period (dot), single quote (apostrophe), left/right bracket (open/close bracket), equals (equal), and minus (hyphen, dash). It also presses the parentheses. Left parenthesis (left paren, open parenthesis, open paren) presses Shift+9, and right parenthesis (right paren, close parenthesis, close paren) presses Shift+0, and the two keys type "(" and ")" on a US keyboard layout.

Other symbol names are not reliable in "press". The shifted symbols (colon, tilde, pipe, question mark, double quote, braces, less than, greater than, plus, underscore) produce the wrong character. The words hash, at, ampersand, asterisk, caret, percent, dollar, and exclamation press nothing. To type any of those, dictate them as punctuation words instead. [Punctuation and Symbols](#punctuation-and-symbols) below handles every symbol.

**Examples**: "press control shift t", "press control two", "press f five", "press alt f four", "press windows d", "press left bracket".

#### Text Formatting

| Say this | What happens | Notes |
|---|---|---|
| uppercase | Converts the selection to UPPERCASE. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "upper case" and "uppercase that" also work |
| all caps / all caps that | Converts the selection to UPPERCASE. Applies only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. |  |
| lowercase | Converts the selection to lowercase. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "lower case" and "lowercase that" also work |
| no caps / no caps that | Converts the selection to lowercase. Applies only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. |  |
| capitalize | Capitalizes the first letter of the selection and lowercases the rest. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "capitalize that" also works |
| cap / cap that | Capitalizes the first letter of the selection and lowercases the rest. Applies only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. |  |
| title case | Converts the selection to Title Case |  |
| snake case | Converts the selection to snake_case |  |
| camel case | Converts the selection to camelCase |  |
| pascal case | Converts the selection to PascalCase |  |
| kebab case | Converts the selection to kebab-case |  |
| compress | Removes the spaces from the selection, joining the words together. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |
| no space / no space that | Removes the spaces from the selected text, joining the words together. Fires only when it is the whole utterance. | Acts on the selected text only; with nothing selected it does not act on the last dictated words |
| bold | Bolds the selection (Ctrl+B). Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "bold text", "bold that", "boldface", "boldface that", "boldface text", "bold face", "bold face that", and "bold face text" also work. Works in apps that support rich text |
| italics | Italicizes the selection (Ctrl+I). Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "italicize" and "italicize that" also work. Works in apps that support rich text |
| underline | Underlines the selection (Ctrl+U). Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "underline that" also works. Works in apps that support rich text |
| parentheses [text] | Wraps the selection in ( ), inserts an empty ( ) pair, or inserts the spoken text wrapped | "parentheses hello" gives "(hello)" |
| brackets [text] | Wraps the selection in [ ], inserts an empty pair, or wraps the spoken text |  |
| braces [text] | Wraps the selection in { }, inserts an empty pair, or wraps the spoken text |  |
| angle brackets [text] | Wraps the selection in < >, inserts an empty pair, or wraps the spoken text |  |
| quotes [text] | Wraps the selection in double quotes, inserts an empty pair, or wraps the spoken text | "quote [text]" also works |
| single quotes [text] | Wraps the selection in single quotes, inserts an empty pair, or wraps the spoken text | "single quote [text]" also works |
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

Formatting commands apply to the current selection. Select first, with the mouse or "select word" / "select line", then say the command. The case and shape transforms cover UPPERCASE, lowercase, capitalize, title case, and the programming styles snake_case, camelCase, PascalCase, and kebab-case. The wrapping commands ("parentheses", "brackets", "braces", "angle brackets", "quotes", "single quotes") enclose the selection in those characters. When you speak them with no selection, they insert an empty pair with the cursor between them. Wheelhouse wraps words spoken after a wrapping word in the same utterance verbatim: it types symbol words such as "colon" literally and does not convert them.

Three commands apply character formatting through the keyboard shortcuts of the host application: "bold text", "italics", and "underline". None of the three needs the safety word, and each fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed.

Forty-eight more commands select a range and format it in one step. Say "bold", "italicize", "underline", "uppercase", "lowercase", or "capitalize". Then say "next" or "previous" ("last" also works), an optional count, and a unit: words, lines, paragraphs, or characters. For example, "bold next three words" or "underline previous line". These commands also need no safety word and fire only as your whole utterance.

#### Navigation

| Say this | What happens | Notes |
|---|---|---|
| go [where] | Moves the cursor without touching the keyboard: go home / go end / go top / go bottom / go left / go right / go up / go down, with counts, word, line, and paragraph units, and "then" chains such as "go up three then go left two", with "grab" steps that select along the way | See the text below the Navigation table for the full move list |
| go up [number] lines / move up [number] lines | Moves the cursor up that many lines. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go down [number] lines / move down [number] lines | Moves the cursor down that many lines. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go up [number] paragraphs / move up [number] paragraphs | Moves the cursor back that many paragraphs (Ctrl and Up Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go down [number] paragraphs / move down [number] paragraphs | Moves the cursor forward that many paragraphs (Ctrl and Down Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go left [number] characters / move left [number] characters | Moves the cursor left that many characters. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "move backward [number] characters" and "go backward [number] characters" also work |
| go right [number] characters / move right [number] characters | Moves the cursor right that many characters. Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "move forward [number] characters" and "go forward [number] characters" also work. "go write [number] characters" and "move write [number] characters" also work, because speech recognition can hear "right" as "write" |
| go left [number] words / move left [number] words | Moves the cursor back that many words (Ctrl and Left Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30 |
| go right [number] words / move right [number] words | Moves the cursor forward that many words (Ctrl and Right Arrow). Fires only when it is the whole utterance. | The number is optional and defaults to 1; counts are capped at 30. "write" in place of "right" also works, because speech recognition can hear "right" as "write" |
| go up [number] times / move up [number] times | Presses the Up Arrow key that many times. Fires only when it is the whole utterance. | The number is optional and defaults to 1; the word "times" can be left out, as in "go up three"; counts are capped at 30 |
| go down [number] times / move down [number] times | Presses the Down Arrow key that many times. Fires only when it is the whole utterance. | The number is optional and defaults to 1; the word "times" can be left out, as in "go down three"; counts are capped at 30 |
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
| scroll down [number] | Turns the mouse wheel down, without moving or pressing the mouse. Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | The number is how many wheel notches to send, up to 50; e.g. "scroll down three". The command turns the wheel without moving the pointer, so it scrolls whatever a real wheel turn would scroll from where the pointer already sits |
| scroll up [number] | Turns the mouse wheel up, without moving or pressing the mouse. Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | The number is how many wheel notches to send, up to 50; e.g. "scroll up three". |
| scroll left [number] | Turns the sideways mouse wheel left, without moving or pressing the mouse. Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | Needs an application that reads the sideways wheel; many do not. The number is how many wheel notches to send, up to 50. |
| scroll right [number] | Turns the sideways mouse wheel right, without moving or pressing the mouse. Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | Needs an application that reads the sideways wheel; many do not. The number is how many wheel notches to send, up to 50. "write" in place of "right" also works, because speech recognition can hear "right" as "write". |
| start scrolling down | Keeps turning the mouse wheel down until you say "stop scrolling". Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | The scroll keeps going with no further speech, so you can read a long page hands-free. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so, in case the stop command is not heard. If Windows refuses to move the wheel, which can happen over a window that runs as administrator, the scroll stops early and shows a different notification. Any scroll command you say while it runs replaces it, so only one scroll is ever going. |
| start scrolling up | Keeps turning the mouse wheel up until you say "stop scrolling". Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | The scroll keeps going with no further speech. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| start scrolling left | Keeps turning the sideways mouse wheel left until you say "stop scrolling". Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | Needs an application that reads the sideways wheel; many do not. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. |
| start scrolling right | Keeps turning the sideways mouse wheel right until you say "stop scrolling". Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | Needs an application that reads the sideways wheel; many do not. Say "stop scrolling" to end it. It also stops on its own after two minutes and shows a notification saying so. "write" in place of "right" also works, because speech recognition can hear "right" as "write". |
| stop scrolling | Stops a scroll that was started with "start scrolling". Fires only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | The words are "stop scrolling" and not "stop" on its own. A command for the single word "stop" would take that word out of everything you dictate. Saying it when nothing is scrolling does nothing at all. |

"go" moves the cursor, and "grab" moves it and extends the selection. You can chain several moves in one utterance with "then". The utterance must begin with "go". "grab" is valid only when chained after a "go" move: "go home then grab to end". When you speak "grab ..." on its own, Wheelhouse types it as dictation.

A single move that names a unit accepts a count of up to five words: "go right twenty three words", "go up three lines", "go left five characters", "go down two paragraphs". You can leave out the unit word. "go right twenty three" moves 23 characters, and "go up three" and "go down three" press the Up Arrow or Down Arrow key three times, the same as "go up three lines".

A count of several words also works in a chain: "go right twenty three then grab left two words". Up and down moves chain too. Each one presses the same keys as the same words when you say them alone: "go up three then go left two" presses the Up Arrow key three times, then moves two characters left. In a chain, "go up" and "go down" take a count, "lines", "paragraphs", or "times" after a count. "grab up" and "grab down" do not select, so Wheelhouse types a chain with them as dictation.

Counts above 30 move 30. Wheelhouse accepts "to", "too", and "for" as sound-alikes for "two" and "four", so "go right to words" moves two words. If Wheelhouse cannot parse any part of a "go" utterance, it types the whole phrase as dictation instead. As a result, an unrecognized phrase does not move the cursor.

#### Punctuation and Symbols

| Say this | What happens | Notes |
|---|---|---|
| period | Types . inline during dictation | "full stop" also works |
| comma | Types , inline during dictation |  |
| colon | Types : inline during dictation |  |
| semicolon | Types ; inline during dictation |  |
| question mark | Types ? inline during dictation | "questioned mark" also works |
| exclamation point | Types ! inline during dictation | "exclamation mark" also works |
| apostrophe | Types ' inline during dictation |  |
| hyphen | Types - inline during dictation | "minus sign" also works |
| dash | Types an em dash (the long dash) inline during dictation |  |
| slash | Types / inline during dictation | "forward slash" also works |
| backslash | Types \\ inline during dictation |  |
| backtick | Types \` inline during dictation | "back tick" also works |
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
| space bar | Types a single literal space inline during dictation | "spacebar" also works |
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
| colin | Mishear tolerance: inserts : when "colin" is the entire utterance. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |
| come | Mishear tolerance: inserts , when "come", "kama", "commer", or "come on" is the entire utterance. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |

Punctuation and symbol words are replacements. When you speak one as part of a sentence, Wheelhouse types the symbol in place of the word. You do not need a pause. Every punctuation and symbol word behaves this way: period, comma, colon, question mark, and the rest.

The default local engine often mishears "comma" and "colon", so Wheelhouse has two built-in corrections. When you speak **"colin"** as an entire utterance, Wheelhouse inserts ":". When you speak **"come"**, **"kama"**, **"commer"**, or **"come on"** as an entire utterance, Wheelhouse inserts ",". Within a longer sentence, Wheelhouse types these words as ordinary dictation. To type one as a standalone word, use "literal come" or "literal colin".

If the recognizer regularly mishears another word, add a personal correction in the Pattern Manager ("patterns"). The correction applies inline like the built-in punctuation words.

#### Application Switching and System

| Say this | What happens | Notes |
|---|---|---|
| activate [app name] | Brings the named application's window forward; when nothing by that name is open, Wheelhouse looks the name up among your installed programs and starts it. Needs no prefix word. When the command cannot be done (no window matches and no program starts, or Windows will not bring the window forward), Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows a notice that explains the failure and types nothing. When Wheelhouse starts a program and its window does not come forward in time, or the wait gets no answer in time, it stops without typing anything or showing a notice, with or without "x-ray". | e.g. "activate outlook"; "activates [app name]" also works; the spoken name must match whole words of a window title; when more than one installed program matches the name, Wheelhouse starts none of them; with "x-ray" first, a notice lists them so you can say the full name, and without it your words are typed as ordinary text |
| browser | Brings your default web browser to the front. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Wheelhouse looks up which browser is your Windows default at the moment you speak |
| notepad | Brings Notepad to the front. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. |  |
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
| maximize | Maximizes the active window. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "maximize window" and "maximize the window" also work |
| minimize | Sends Windows and Down Arrow to the active window. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "minimize window" and "minimize the window" also work. That one keystroke has two results: it takes a maximized window back to its normal size, and it minimizes a window that is already at its normal size. "restore window" sends the same keystroke. |
| desktop | Shows the desktop (Windows+D). Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "show desktop" and "show the desktop" also work |
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

"activate [app name]" brings the named application's window forward. When nothing by that name has a window open, Wheelhouse starts the program instead of doing nothing. It runs a pattern whose target is a program file (.exe) directly. It looks up a spoken name among your installed programs, by Start menu shortcut and by the names that Windows itself resolves. Wheelhouse also finds programs that you installed from the Microsoft Store. It uses the name that the Start menu shows or the name that the Store lists, for example "terminal" or "windows terminal".

When a Store program is already running, Wheelhouse brings it forward instead of starting it again, even when its window title does not contain the name. Wheelhouse prefers an exact name. If no exact name matches, it uses a name that begins with the words you said. If more than one installed program matches, or none matches, Wheelhouse starts nothing, types the words you said as ordinary text, and shows no notice. If you said the safety word first, Wheelhouse types nothing and shows a notice instead. The notice lists the matching names, so you can say the full name of the one you meant, or it says that no program matched.

The spoken name must match whole words of a window title. For example, "show art" brings forward a window with the title "Art class - Word" and not a window with the title "Start menu". "switch to [app name]", "show [app name]", "minimize [app name]", and "maximize [app name]" work the same way. They need no safety word, and Wheelhouse types your words in the same cases when it cannot do the command.

"x-ray go to [app name]" and "x-ray close [app name]" (also "exit" and "quit") need the safety word. One reason is that "go to" also begins the cursor-movement commands such as "go to end of line", which need none. Another reason is that a close command can discard unsaved work.

When any of these commands starts a program because no window matched, Wheelhouse waits for the new window to come forward. If the window does not come forward in time, or the wait gets no answer in time, Wheelhouse stops without typing anything or showing a notice. It does this with or without the safety word. "minimize", "maximize", and "close" send their key only after the new window comes forward, so they send no key in that case. The built-in "notepad" and "browser" take the program-file path, and "browser" resolves to the Windows default browser when spoken.

The System commands operate on windows and on Windows itself. Seven need the safety word: "x-ray close window", "x-ray close tab", four searches, and the short Google form "x-ray search for [text]", where the word "for" is optional. The four searches are "x-ray search windows for [text]", "x-ray search on google for [text]", "x-ray search on bing for [text]", and "x-ray search on youtube for [text]". The close window command needs the safety word because it discards unsaved work. Every other System command works without the safety word, and each of those fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed. Those include "zoom in", "zoom out", "create tab", "create window", "windows settings", which opens Windows Settings, "maximize", "minimize", and "desktop", which shows the desktop.

In most browsers "create tab" (Ctrl+N) opens a new window rather than a tab. In most browsers "create window" (Ctrl+Shift+N) opens a private or incognito window.

#### Mouse Control

| Say this | What happens | Notes |
|---|---|---|
| show grid | Lays a numbered three-by-three grid over the screen the front window is on, for moving the pointer by voice | Opening the grid removes the numbered overlay; the two are never on screen together. "apply grid" also works. "show grid lines on the chart" is not this command: it is read as "show" plus a window name, and when no window has those words in its title it is typed as ordinary text. See the text below the Mouse Control table |
| hide grid | Closes the grid without clicking anything | "dismiss grid" and "dismissed grid" also work. "hide grid lines before printing" on its own is typed as ordinary text, not treated as this command. |
| grid next screen | Moves the open grid to the next monitor, restarted at full size |  |
| [number one to nine] | Redraws the open grid inside that cell, narrowing the target. With the grid closed and the numbered overlay showing, the number clicks the control with that label. With neither showing, numbers are ordinary dictation and are typed normally. | "to" and "too" also mean two, and "for" also means four, because the speech engine can write those words for the spoken numbers |
| number [one to nine] | Same cell narrowing with the word "number" first ("number five"), which speech engines hear more reliably than a single word. With the grid closed and the numbered overlay showing, the phrase clicks the control with that label. With neither showing, the phrase is ordinary dictation and is typed normally. | "numbers" in place of "number" also works, as in "numbers five"; "to" and "too" also mean two, and "for" also means four, because the speech engine can write those words for the spoken numbers |
| click / tap / right click / double click / triple click | Clicks at the center of the grid that is on the screen and closes the grid; a number in the same utterance narrows first ("click five"). With no grid open, the words click at the current pointer position; a triple click there selects a line of text. Say "literal click" to type the word. When voice clicking is turned off, the words are typed normally. |  |
| mark | Pins the start point of a drag at the center of the grid that is on the screen and restarts the grid so you can navigate to the destination |  |
| drag | Holds the left button at the marked point, moves gradually to the center of the grid that is on the screen, and releases -- a complete drag and drop | Requires a "mark" first; without one, a notice explains the step |
| move here / go here | Moves the pointer to the center of the grid that is on the screen without pressing anything, for hover menus and tooltips |  |

The **mouse grid** moves the pointer to any point on screen by voice. This includes points that a click by name cannot reach: applications that hide their controls from accessibility tools, canvas areas where you draw, maps, and games.

Say **"show grid"** to put a numbered three-by-three grid over the screen that has the front window. When you say a cell number, **one through nine**, Wheelhouse redraws the grid inside that cell. Repeat until the center is on the target. You can say the word "number" before each number, because engines hear "number five" more reliably than a single word. Then say the action:

| Say | What happens |
|---|---|
| click | left click at the center of the grid that is on the screen |
| double click | double click there |
| triple click | triple click there, which selects a line of text |
| right click | right click there |
| move here / go here | moves the pointer there without clicking |
| mark | pins the start point of a drag and restarts the grid |
| drag | drags from the pinned point to the center of the grid that is on the screen |
| hide grid | closes the grid without clicking |
| grid next screen | moves the grid to the next monitor |

A number in the same utterance narrows first. "click five" narrows into cell 5, then clicks its center. All actions close the grid except "mark", "grid next screen", and a "drag" with no mark.

**With no grid open**, "click", "tap", "right click", "double click", and "triple click" click at the pointer's current position when you speak them as your whole utterance. Inside a longer sentence, Wheelhouse types the words. To type one of them on its own, say "literal click". When the voice click commands are off (`enabled` in the click section of the settings file), Wheelhouse types the words.

**Drag** takes two steps. Navigate to the point to drag from and say **"mark"** -- a pin appears and the grid restarts at full size. Navigate to the destination and say **"drag"**. Wheelhouse presses the left button at the pin, moves the pointer gradually, and releases the button. Many applications ignore a pointer that jumps instantly, which is why the pointer moves gradually.

Wheelhouse holds no button while you navigate, so a pause or a misheard word between the steps cannot drop anything in the wrong place. "drag" without a "mark" shows a notice and presses nothing. Wheelhouse forgets the pin when the grid closes.

While the grid is open, a spoken number on its own belongs to the grid, and Wheelhouse does not type it. When the grid is closed and the numbered overlay is on the screen, a number that you speak as your whole utterance clicks the control with that label. When neither is on the screen, the same words are ordinary dictation.

The grid and the numbered overlay are never open together: "show grid" removes the numbers, and "show numbers" closes the grid. The grid narrows no further when cells reach `grid_min_cell_px` (click section of the settings file), because a cell that small is already within the precision of a click. The drag speed is `drag_duration_ms`, in the same section of the settings file.

No commands move the pointer continuously (no "mouse up" / "mouse down"). The grid places it at a chosen point instead. Wheelhouse maps volume and screen brightness to the thumb wheel of a Logitech MX-series mouse. See [Plugins](#plugins).

#### Voice Element Clicking

| Say this | What happens | Notes |
|---|---|---|
| click [name] | Clicks the button, link, menu item, or other control with that name; add a role word to narrow the search, or give the overlay number instead of a name while the numbered overlay is showing. Needs no prefix word: say "click cancel". "clicks" and "tap" work too. When no control has that name, Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows the "No match" notice and types nothing. | See the text below the Voice Element Clicking table |
| show numbers | Paints a number on every clickable control in the front window | By default the numbers go away after you click one or change the front window, as in Windows Voice Access; say "hide numbers" to remove them sooner. The Settings window check box "Keep numbers on after a click or window change" keeps them on: Wheelhouse then shows new numbers after a click and after every window or menu change. "apply numbers" and "show numbers here" also work. "show numbers in the report" is not this command: it is read as "show" plus a window name, and when no window has those words in its title it is typed as ordinary text. |
| hide numbers | Removes the numbers | "dismiss numbers", "dismissed numbers", and "hyde numbers" also work. "hide numbers on the chart" on its own is typed as ordinary text, not treated as this command. |
| right click [name] / double click [name] | Clicks the named control with a right click or a double click instead of a normal click; also works with a number while the numbered overlay is showing ("right click three"). Presses a real mouse click at the control's center, with the same checks as a normal click. Useful where a normal click is not enough: File Explorer items open on a double click, and a right click opens the context menu. |  |

Wheelhouse can click buttons, links, menu items, and other on-screen controls. A control is selected in one of two ways: by its **name**, or by a **number** that Wheelhouse displays on every clickable control. To click a number, say "click five". The numbered overlay covers controls with no spoken name, such as icon-only toolbar buttons, and cases where several controls share a name.

**Click by name**: say "click", then the name of the control. You can put "the" before the name, and Wheelhouse ignores it. You can put a role word after the name to restrict the search to one kind of control. Plain "click [name]" needs no safety word: say "click cancel". "clicks" and "tap" work the same way.

Any sentence that opens with "click" or "tap" reaches this command, so Wheelhouse looks for the control first. When no control has that name, it types the words you said as ordinary text and shows no notice. If you say the safety word first, as "x-ray click cancel", it shows the "No match" notice instead and types nothing. "right click [name]" and "double click [name]" need no safety word.

**Role words**: **button**, **link** (a hyperlink), **menu** (a menu item), **tab**, **checkbox** (or **check box**), and **box** / **field** / **input** (a text entry field). With no role word, Wheelhouse considers any clickable control that matches the name. If you speak a role word with no name, as in "click button", Wheelhouse treats it as a name and searches for a control named "button".

**Right click and double click**: "right click [name]" and "double click [name]" work wherever "click [name]" works. "right click three" / "double click three" work while the numbered overlay is on the screen. These commands press a real mouse click at the center of the control, with the same safety checks as a normal click. They cover what a normal click cannot do. A right click opens the context menu of a control, and a double click opens File Explorer items.

**The numbered overlay**: "show numbers" displays a number on every clickable control in the front window. "click three" clicks the control labelled 3, and "hide numbers" removes the numbers. "escape" also removes the numbers and closes the mouse grid, after it presses the Escape key. By default the numbers go away after you click one or change the front window, as in Windows Voice Access.

The Settings window check box "Keep numbers on after a click or window change" (the `overlay_keep_numbers` key in the `[click]` section) keeps the numbers on. With this setting on, after you click a numbered control, Wheelhouse repaints the numbers in place, and they follow whichever window is in front. When "click [name]" matches more than one control closely, the numbers appear on those candidates only.

With the mouse grid closed, a number that you speak as your whole utterance ("seven", "number seventy four") also clicks the control with that label. While the grid is open, a bare number belongs to the grid. A control whose own name is a number, such as a calculator key, is found when you say that number, as in "click seven button".

When you click the mouse or turn its main scroll wheel, Wheelhouse moves the numbers to the new positions of the controls. It moves them about half a second after the last click or wheel movement. Wheelhouse does the same after the scroll commands. If a page changes by itself or you scroll with the keyboard, say "show numbers" again.

When the numbers are on the screen over the Pattern Manager, they follow its list. When you filter, add, remove, edit, expand, or collapse rows, Wheelhouse numbers the list again. When you click the number of a pattern or category row, Wheelhouse selects that row and shows its details.

**Outcomes**: a successful click produces no notice. A failure produces a brief notice near the floating button and the tray icon. There are three kinds of failure. Wheelhouse shows **not found** ("No match for [name]") only when you said the safety word first. Otherwise Wheelhouse types the words as ordinary text. The numbered overlay is the alternative.

For an **ambiguous** failure, the numbered overlay opens on the candidates. The "Found [A] and [B] -- be more specific" notice appears only when the overlay cannot open. For a **could not complete the click** failure, the notice states the reason: control disabled, click timed out, or the overlay is stale and you must apply it again. Wheelhouse limits the rate of notices, so repeated failures do not produce repeated notices.

#### Wheelhouse Control

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening: Wheelhouse listens only while you hold the floating button | A notification confirms the switch |
| click to talk mode | Switches back to toggle listening (click to start, click to stop) -- the default |  |
| stop listening | Switches listening off, as clicking the floating button or the tray icon does while it listens. Applies only when the words are the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | In toggle mode, say the wake word ("computer") to switch listening back on; while sound that this computer plays keeps listening paused, click the floating button or the tray icon instead. In push-to-talk mode the wake word ends only the idle pause, so the way back is the next hold of the floating button. A notice confirms that listening is off |
| help | Opens the Wheelhouse Assistant (the official online help) in your browser, after a short explanation window. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "open voice access help" also opens it. Uses the assistant_url setting under [ai.help]; if blanked, the command shows a notice that online help is not configured and no window appears. Release 1.2.1 and earlier named the setting gem_url; updating with the installer renames it to assistant_url and replaces an old assistant address with the current one. The explanation window is the same one the Help menu item shows, and both take the same route through the Logic process; ticking "Do not show this again" and selecting Assistant sets explain_before_open to false under [ai.help], which turns the window off for the command and the menu item alike. If Windows cannot start a browser, the notice "Wheelhouse could not open your browser." appears |
| patterns | Opens the Pattern Manager. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "pattern", "pattern manager", "what can I say", "show commands", "show all commands", and "show command list" also open it; see "Selected Commands in Detail" in the Voice Commands section |
| settings | Opens the Settings window, which holds the settings of the right-click menu and seven more. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | "setting" also works. The Settings item in the right-click menu opens the same window. The window has 14 settings: Speech Enabled, Show Floating Button, Interim Results, Push-to-Talk Mode, Debug, the Speech-to-Text Provider, and the AI Model from the menu; and Check for updates, Log transcripts, the replacement timeout, the command timeout, audio suppression, the wake word, and Keep numbers on after a click or window change. Every control is a check box, a radio button, or a button, so "show numbers" and "click" with a number change any setting by voice. The timeouts have Decrease and Increase buttons that change the value by 100 milliseconds, between 200 and 3000. Interim Results saves to config.toml. Speech Enabled and Debug do not save. Log transcripts, both timeouts, audio suppression, and the wake word show the text "takes effect after restart". The Restart WheelHouse button restarts Wheelhouse. Keep numbers on after a click or window change saves to config.toml and applies to the next click or window change with no restart. The Help button opens a window that explains each of the 14 settings. The Other settings group also holds a Training word... button, which is not a setting; it opens the Your training word window, where the Parakeet speech engine's training word is recorded. Turning speech off while the wake word is off leaves no spoken way to turn it on again. All other settings stay in config.toml |
| check for updates | Opens the About Wheelhouse window and checks GitHub for a newer release. Applies only when the phrase is the whole utterance. Inside a longer sentence, Wheelhouse treats the words as dictation. | "check for update" also works. The Check for updates button in the About Wheelhouse window starts the same check. The check runs even when the automatic check is off |
| record my training word | Opens the training-word window, where you record the word hello once so that Wheelhouse keeps the first sound of a phrase after a pause | only the Parakeet speech engine uses it; See "Your training word" in the Speech Engines section |
| x-ray fix | Sends the selected text to the configured AI server for formatting correction -- capitals, numbers, amounts of money, common abbreviations, and obvious punctuation -- then replaces the selection with the result. The AI is told not to reword the text or add or remove words | "x-ray fixed" also works. Requires the AI server to be configured and reachable; Wheelhouse shows its progress and outcome on screen rather than out loud, and always preserves your original text on any failure |
| simplify | Rewrites the selected text in plain language, using shorter sentences and simpler words. Keeps every fact and leaves the layout alone -- line breaks, indentation, bullet marks, numbering, code lines and addresses come back unchanged. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Same AI server and same safeguards as "x-ray fix"; the selection comes back as plain text, so formatting applied in a word processor is lost |
| shorten | Rewrites the selected text more briefly, cutting repetition and padding. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Same AI server and same safeguards as "x-ray fix" |
| x-ray make formal | Rewrites the selected text in a formal register, avoiding contractions and casual wording | Same AI server and same safeguards as "x-ray fix" |
| pirate | Rewrites the selected text the way a pirate would say it. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | Ships as a worked example: it is the same action as the three above with a different sentence in the pattern file. See "Selected Commands in Detail" in the Voice Commands section for writing your own. |
| x-ray translate to [language] | Translates the selected text into the language you name, for example "x-ray translate to spanish" or "x-ray translate to brazilian portuguese". Keeps every fact and leaves names and numbers as they are. | Same AI server and same safeguards as "x-ray fix"; say the language in English and in lower case, as one or more plain words with no punctuation. How good the translation is depends on the model you have configured. |
| cancel fix | Cancels an in-progress fix or rewrite. Needs no prefix word. When no AI request is running, Wheelhouse types your words as ordinary text and shows no notice. If you said "x-ray" first, it shows the notice "No AI job is running." and types nothing. |  |
| boost | Adds the selected text to the speech recognition hints. Applies only when the word is the whole utterance. Inside a longer sentence, Wheelhouse treats the word as dictation. | See "Selected Commands in Detail" in the Voice Commands section -- works only when the running engine applies hints: Google always, Parakeet only with hint biasing on. Otherwise Wheelhouse types the word as dictation |

These commands control Wheelhouse itself: listening modes, help, personal patterns, and the AI features. "push to talk mode" and "click to talk mode" switch between the two listening modes. The table above describes "x-ray fix". It shows its progress on screen rather than out loud ("Correcting..."). Then it shows a notice with the outcome (for example "Done.", "No changes needed." or "Cancelled."). If the request fails, it leaves the original text in place.

Five further commands rewrite the selection instead: "simplify", "shorten", "x-ray make formal", "pirate", and "x-ray translate to [language]". See "Rewriting the selected text" under [Selected Commands in Detail](#selected-commands-in-detail), which also covers how to add others.

"boost" adds the selected text to the speech recognition hints when the running engine applies hints (see "boost" below). "patterns" opens the Pattern Manager. "settings" opens the Settings window described under [Floating Button and Tray Icon](#floating-button-and-tray-icon). "help" opens the Wheelhouse Assistant in the default browser after a short explanation window. "cancel fix" stops a correction or rewrite that is still running, and it needs no safety word either. Every other command named in these three paragraphs without the "x-ray" prefix needs no safety word, and each of those fires only as your whole utterance: the same words inside a longer sentence are typed, not obeyed.

Three further commands act on text through the host application. "x-ray find [text]" opens the find box of the host application and searches for the words that you speak. "replace" opens the find-and-replace box of the host application. "search" copies the selection and searches the web for it in the default browser.

"stop listening", said as the whole utterance, switches the microphone off. No voice command switches it on, because Wheelhouse transcribes nothing while listening is off. In toggle mode the wake word ("computer") is the voice route back on, and you can also click either the floating button or the tray icon. While sound that this computer plays keeps listening paused, the wake word does not turn listening on. Click the floating button or the tray icon instead. In push-to-talk mode the wake word ends only the idle pause, so after "stop listening" the way back is the next hold of the floating button. Wheelhouse listens only as long as you hold that button, and the hold works on the floating button alone. In push-to-talk mode, a click on either surface has no effect.

The right-click menu's **Speech Enabled** item is the exception: in push-to-talk mode it switches listening on and changes the mode to toggle mode.

In this release, the in-app help chat window is disabled, and the voice patterns that opened the window are off. "help" opens the Wheelhouse Assistant in the browser. See [Getting Help](#getting-help).

### Selected Commands in Detail

**"literal [words]"**

Say "literal" followed by the text to type. Wheelhouse inserts those words and does not match them against any command or replacement pattern. Use this to dictate a phrase that would otherwise trigger a command. "literal copy" types the word "copy" instead of copying. "literal period" types the word instead of a full stop. "literal new line" types the phrase instead of a line break.

"literal" takes effect wherever it appears in an utterance. Wheelhouse types everything after it exactly as spoken, and it does not type "literal" itself. To type the word "literal", say "literal literal".

**"boost"**

When the recognizer mishears a specific word again and again (typically a name, a product, or a technical term), select the word with the mouse or with "select word". Then say **"boost"** by itself. When the running engine applies hints, Wheelhouse saves the selection as a recognition hint in a shared hints file. That file stays across restarts, so you need to boost each word only once. The limit for a hint is 100 characters. Boost words or short phrases, not sentences.

**Only an engine that applies hints accepts "boost".** **Google Cloud Speech-to-Text** applies saved hints without further configuration. **Parakeet, the default engine**, applies them only when hint biasing is on. Hint biasing is off by default because it slowed recognition by roughly 25 percent per utterance in the project's measurements.

To switch hint biasing on, set enabled = true under [hotwords] in Parakeet's own config file. Then restart Wheelhouse. Expect the slower recognition. An update of Wheelhouse sets it back to false.

With biasing on, Parakeet accepts "boost" when its hints loaded, or when no hint exists yet. If its hints failed to load, its ready notice says why.

**Under an engine that does not apply hints, "boost" is dictation.** Wheelhouse types the word "boost" in place of the selection. It copies nothing, saves no hint, and shows no notice. In most applications, "undo" removes the typed word and restores the selected text.

The same rule covers any pattern with the action "Teach word to speech engine". This is true for a pattern you made yourself too. The Pattern Manager's "Try it" field gives the same answer: under such an engine it reports no command for "boost".

**"patterns" (the Pattern Manager)**

This opens the **Pattern Manager**. The Pattern Manager lists every voice command and text replacement, and it groups them by category. When you select an entry, the Pattern Manager shows its trigger phrase, its action, and if it requires the safety word. Tick "Only my patterns" (Alt+M) to list only the patterns that you added, duplicated, or customized. This option works together with the filter box.

The Pattern Manager can do these things. It can **view** any pattern, including every built-in one. It can **create** personal patterns, such as a shortcut that types an email address, a correction for a misheard word, or a command that opens a program. It can **edit** and **delete** user-created patterns. It can **customize** a built-in pattern: a personal copy with the same trigger overrides the built-in pattern, and when you delete that copy, Wheelhouse restores the shipped behavior. It can **change the safety word** from "x-ray" to another word.

To write your own expression or add several actions (advanced mode), click Add Pattern and choose **Create an advanced command**. You can also tick the **Advanced** check box in the editor. A new pattern of yours runs before the built-in patterns. A built-in pattern that you edit keeps the built-in's place in the order. Wheelhouse tries the patterns in order, and the first match wins. As a result, a new pattern of yours beats a built-in pattern that matches the same words, and an edited built-in runs only when no pattern ahead of it matches.

Personal patterns are in a separate per-machine file. Upgrades preserve this file, and Wheelhouse never modifies the shipped patterns file. The personal patterns file is `user_patterns.toml`, in the folder `%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\data`. It does not exist until you first save a pattern or change the safety word, and the Pattern Manager is the tool that edits it.

**Numbers in your own patterns.** In advanced mode, write `(\d+)` where a number goes in the trigger. Wheelhouse then accepts the number as digits or as words, so "delete 3" and "delete three" both work.

An action that takes a count, such as a key press, reads "seven" and "7" as the same number. If the words of the first number are not a number, the pattern does not match. A text replacement types the number as you said it: "page seven" types "seven", and "page 7" types "7".

Two cautions apply. The words "for", "to", and "too" also count as numbers. So a replacement with the trigger "page" plus a number also changes "turn the page for me". Start the trigger with `^` to make the pattern match only a whole utterance. A spoken "number" before the number becomes part of the number text. Do not put the word "number" in the text that the pattern types.

**How to run a program from a pattern.** Two actions run programs. **Run a program** starts a program or a command line and does not wait for it to finish. **Run a program and capture its text** runs one program without a shell and waits for it to finish. It keeps the text that the program printed for the steps after it. A Python script runs the same way, through the Python interpreter.

For example, a pattern that inserts what `C:\scripts\example.py` prints needs two steps in advanced mode. The first step uses **Run a program and capture its text**, with `python` in its program field and `C:\scripts\example.py` in an argument field that **Add argument** creates. The second step uses **Insert text**, with `run_capture` as its text. That is the name of the action of the first step, and it stands for the text that the script printed. The step inserts that text at the cursor.

The capture waits for the number of seconds in its timeout field, or for 10 seconds when that field is empty. It never waits longer than 60 seconds. The 10-second default is the setting run_capture_timeout_default_s under [actions]. The printed text can contain at most output_cap_chars characters. This is a setting under [actions], and its default is 10000.

If the output is longer, the step fails. Wheelhouse does not cut the output short. The step also fails when the program is still running at the end of the wait. In that case Wheelhouse stops the program. The step also fails when the program exits with an error code.

A failed step stops the pattern, so Wheelhouse inserts nothing. Wheelhouse types the spoken words as ordinary dictation instead.

The [Action Reference](https://wheelhouse-project.org/reference.html#action-reference) lists every action and its parameters.

**Rewriting the selected text**

With text selected, **"simplify"** sends the text to the AI server for a rewrite in plain language and replaces the selection with the result. **"shorten"** removes repetition and padding. **"x-ray make formal"** removes contractions and casual wording. **"pirate"** rewrites the text in pirate speech. **"x-ray translate to [language]"** translates the selection into the language that you name, for example "x-ray translate to spanish" or "x-ray translate to brazilian portuguese". Wheelhouse recognises multi-word names.

All five use the same AI server as "x-ray fix", and they leave the original in place if the request fails. They show "Rewriting..." where "x-ray fix" shows "Correcting...". Then they show an outcome notice.

A rewrite pastes back plain text, so the rewritten part loses the text styles of a word processor: bold, italics, a font, and a colour. Wheelhouse preserves layout: line breaks, blank lines, indentation, bullets, numbered lists, and non-sentence lines such as a code line or a postal address return unchanged.

**How to add a rewrite command.** The five commands are one action with a different instruction for each command, so you can add more commands from the Pattern Manager without a change to the program code. Create a pattern. Choose the **Rewrite text with AI** action. Set its parameter to a sentence that tells the AI the style that you need.

For example, to rewrite text at a reading level that a younger reader can follow, create a pattern with the trigger **reading level** and this instruction:

> Rewrite this text so a ten-year-old could read it. Use short sentences and everyday words, and explain any term a ten-year-old would not know. Keep every fact. Return only the rewritten text.

To use the new command, select a paragraph and say "x-ray reading level". The command then rewrites the paragraph.

The instruction has two constraints. First, **describe the style only** -- Wheelhouse itself supplies the wording that preserves layout and stops instructions inside the selected text from redirecting the AI. If you duplicate either, the result gets worse. Second, **end with "Return only the rewritten text."** Without that sentence, the AI can add a sentence of commentary before the text, and Wheelhouse then pastes the commentary into the document.

**"cancel fix"** stops a request that is still running, which can be a correction or any of the rewrites, and Wheelhouse pastes nothing. It needs no safety word. When no request is running, Wheelhouse types the words as ordinary text. If you said the safety word first, it shows the notice "No AI job is running." and types nothing.

**"help"**

This command opens the Wheelhouse Assistant, the project's online help, in the default browser. In the Assistant you can ask questions in plain language. The address is the assistant_url setting in [ai.help], which points at the assistant by default. Release 1.2.1 and earlier named the setting gem_url. When you update with the installer, the installer renames it to assistant_url and replaces an old assistant address with the current one. [Getting Help](#getting-help) gives the notice that appears when the address is missing.

A short explanation window comes first. It is the same window that the **Help** menu item shows. It explains that the Wheelhouse Assistant runs on Google's Gemini Notebook, so you must sign in with a Google Account. Its **Assistant** button opens the browser.

If you tick **Do not show this again** and select **Assistant**, Wheelhouse sets explain_before_open to false under [ai.help]. After that, "help" opens the browser directly. [Getting Help](#getting-help) explains when the window appears again after **Cancel**, and how to bring it back.

## Speech Modes

Wheelhouse has no command mode and no dictation mode to switch between. Wheelhouse classifies each phrase when the phrase arrives. The first word of the phrase decides if the phrase can be a command. Most commands run only when the whole phrase is the command.

### Classification of spoken input

- **Command**: the utterance matches a voice command, and Wheelhouse runs the command. "undo" presses the undo shortcut. "delete five" deletes five characters. "erase five" does the same, because "erase" is a synonym for "delete". Wheelhouse types nothing.
- **Dictation**: Wheelhouse types the utterance into the focused text field. "dear Sarah thank you for the update" appears as those words.
- **Inline replacement**: Wheelhouse replaces certain words in dictation with symbols or corrected spellings. "hello comma world" produces "hello, world". Wheelhouse replaces the word "comma" with the punctuation mark and does not type the word.

### Word position determines classification

- **The first word of a phrase is a candidate command.** A phrase is what the speech engine reports as one utterance. A phrase normally ends when you pause. Wheelhouse checks the first word of a phrase against the set of known command openings. If the word can begin a command, Wheelhouse holds it to see if the next words complete a command. With the shipped settings, Wheelhouse holds most openings for 700 ms. A name or other free text can follow some openings, such as "click", "go" and "press". Wheelhouse holds these openings for up to five seconds. After the safety word, "select" is also such an opening. At the start of a phrase without the safety word, "select" gets the 700 ms hold. If you still speak when a 700 ms hold ends, the hold continues until the next word arrives or the phrase ends. The hold lasts at most five more seconds. Most commands, such as "undo" and "delete five", run only when they are the whole phrase. If the words match no command, Wheelhouse types them as ordinary text. Wheelhouse drops words in the cases that the safety word and streaming insertion descriptions below name. Wheelhouse also discards a phrase that begins with "okay Google", "ok Google", or "hey Google", and it removes hesitation sounds such as "um" and "uh". The Voice Commands section names these two filters.
- **Wheelhouse types a command word in the middle of a phrase as text.** Wheelhouse types "I want to delete five items" in full, with the word "delete", because "delete" did not begin the phrase. A short pause that does not end the phrase does not change this. The rule does not mean that only the first word of a phrase can belong to a command. When a phrase begins with a command, the words after the first word are part of that command: "go home then grab to end" is one command.
- **Replacement words apply in any position.** Wheelhouse replaces words such as "comma" and "period" when they occur first, last, or mid-sentence, because their purpose is to appear within dictation.

### Commands that need the safety word

The safety word protects commands that are disruptive or hard to undo if they run while you dictate. These commands run only when the utterance begins with "x-ray", as in "x-ray close window". Other commands do not need it. The safety word follows the same position rule. "x-ray" has its special meaning only as the first word of a phrase. Wheelhouse types it as text anywhere else.

If words that are not a command follow "x-ray" without a pause, Wheelhouse types the whole phrase as text. The phrase includes "x-ray". If you pause after "x-ray" for longer than the command wait (700 ms with the shipped settings), Wheelhouse types only the words that you speak after the pause.

### Streaming insertion

Wheelhouse inserts recognized words while speech continues. It does not wait for the utterance to end. There are two exceptions, and both are brief holds that normally last fractions of a second. The first hold is at the start of a phrase, while Wheelhouse evaluates a command match. This hold can last longer while you still speak, as described above. The second hold is similar and happens around replacement words.

A third exception is not a hold. Wheelhouse reads the window to put numbers on the controls, or to find a control named in a "click" command. If you speak a word during that read, Wheelhouse drops the word instead of typing it, and does not type it afterwards. Wheelhouse drops the word deliberately. If Wheelhouse held the word until the read finished, Wheelhouse would type it seconds later, into whatever had focus by then. The refusal lasts no longer than the read itself, and it cannot continue without limit.

### Chaining cursor moves with "then"

You can chain cursor movements and text selections into one phrase with "then":

- "go home then grab to end" moves to the start of the line and then selects to the end of the line.
- "go top then grab to bottom" moves to the top of the document and then selects to the bottom.

You can chain only the "go" (move the cursor) and "grab" (extend the selection) navigation commands. You speak all other commands as separate phrases. These commands include copy, paste, and the commands that switch windows.

### Wheelhouse and Windows Voice Access

Windows Voice Access is the voice control that Microsoft includes in Windows 11. In its default mode, Voice Access also accepts commands and dictation without a mode switch. The absence of a mode switch is for this reason not a difference between Wheelhouse and Voice Access. Voice Access also has a commands mode and a dictation mode, which the user enables by voice. Wheelhouse has no such modes.

Some features work the same way in both programs. Both programs let you type words that would otherwise run a command. You say the word type or dictate before those words. "type undo" types the word "undo" and does not run the undo command. Wheelhouse also accepts "literal undo".

Both programs can put numbers on the clickable controls ("show numbers"). Both programs can show a grid for the mouse ("show grid").

Voice Access requires Windows 11, version 22H2 or later. Wheelhouse also runs on Windows 10.

This document describes Wheelhouse. It cannot list every Voice Access feature, and it does not describe how Voice Access decides between a command and dictation in its default mode. For Voice Access details, see Microsoft's documentation, which starts at "Get started with voice access" (https://support.microsoft.com/topic/get-started-with-voice-access-bd2aa2dc-46c2-486c-93ae-3d75f7d053a4). When this document does not mention a Voice Access feature, that does not mean that Voice Access lacks the feature.

## Interaction Modes

The speech modes, which the section above describes, govern how Wheelhouse classifies recognized words. Interaction modes govern when Wheelhouse listens. There are two interaction modes. You can switch between them at any time.

### Toggle mode

Wheelhouse listens continuously while speech is on. One click on the floating button, or one left-click on the tray icon, stops Wheelhouse from listening. Another click makes it listen again. This is the mode for hands-free operation. When Wheelhouse listens, you need no further input device.

In toggle mode, a press and hold of the floating button for about a fifth of a second or longer makes Wheelhouse listen. It listens for as long as you hold the button. When you release the button, listening returns to its state before the hold. If listening was off, it is off again. If listening was on, it stays on. This is useful when Wheelhouse normally does not listen and you want to speak a single command.

### Push-to-talk mode

Wheelhouse listens only while you hold down the floating button. Press and hold the button to speak. When you release the button, Wheelhouse stops listening. During the hold, Wheelhouse mutes the computer's speakers. This stops sound from a video or from music from reaching the microphone, where Wheelhouse would transcribe it. Wheelhouse restores the previous volume on release.

If the volume choice changes from System Volume to the Sonos TV speaker during the hold, Wheelhouse restores the previous volume on release. Then it sets the Windows volume to 50 percent. [Plugins](#plugins) describes this.

The System Volume plugin mutes the speakers ([Plugins](#plugins)). The plugin is enabled by default. If you disable it, you also disable the muting. In this mode a single left-click on the tray icon has no effect. The hold works on the floating button.

Four constraints apply:

- **Audio already playing.** When the sound pause applies (see `ENABLE_AUDIO_SUPPRESSION` in [Plugins](#plugins)), a hold can still listen over the sound. This works only when the speakers are really muted. The hold mutes the speakers and ignores the pause at once. It continues to ignore the pause only if the System Volume plugin confirms the mute. No confirmation arrives when the plugin is disabled, or when no audio device answers. If no confirmation arrives, the pause applies to the hold about half a second after the hold starts. The button shows that listening is off. In that case, pause the audio first. Listening resumes when the sound-level check notices the silence. This can take up to about thirteen seconds after the audio stops. A Sonos speaker playing music still pauses a hold, because muting this computer cannot silence it.
- **A program with sole control of the speaker.** While a program has sole control of the speaker, a hold does not start listening, because push-to-talk does not end that pause. Wheelhouse reports "Another app has taken sole control of the speaker. Listening is paused until it lets go." The pause ends when the program releases the speaker. See `ENABLE_AUDIO_SUPPRESSION` in [Plugins](#plugins).
- **Safety release.** If Wheelhouse never registers a release, it stops listening after 30 seconds and restores the previous audio state. This way the microphone does not stay open and the speakers do not stay muted. If that cutoff interrupts long dictations, raise ptt_safety_timeout_seconds in the [speech] section of the settings file.
- Push-to-talk requires a hand on the mouse or a finger on a touchscreen, so it is not hands-free.

### How to switch between the modes

Any of the following, at any time:

- **By voice**: "push to talk mode" switches to push-to-talk. "click to talk mode" switches back to toggle mode.
- **From the menu**: right-click the floating button or the tray icon. Both open the same menu. Click "Push-to-Talk Mode". A checkmark on that item shows that push-to-talk is active. The Push-to-Talk Mode check box in the Settings window does the same. The menu item "Settings" opens that window.
- **Double-click**: double-click the floating button or the tray icon. This alternates between the two modes.
- **At startup**: the interaction_mode setting in the [speech] section of the settings file selects the mode that Wheelhouse starts in. The value is either "toggle" or "push_to_talk". The voice, menu, and double-click switches change the mode while Wheelhouse runs.

### Choosing a mode

Toggle mode is the default and supports hands-free operation. Push-to-talk suits a noisy room. It also suits an environment where Wheelhouse would transcribe other people's speech or the computer's own audio. It also suits occasional voice input where you want Wheelhouse to recognize nothing between holds.

## Floating Button and Tray Icon

Wheelhouse has two on-screen controls: a small round button that floats above other windows, and the Wheelhouse icon in the system tray near the clock. Both controls switch listening on and off. Both switch between the two interaction modes. Both open the same right-click menu. The floating button also reports the current state. You can move and resize only the floating button.

### The floating button

The button is a coloured circle that stays above other windows. Its colour reports the current state:

- **Dark grey**: the start of Wheelhouse is not complete, and Wheelhouse does not yet know if the speech engine is ready.
- **Light grey**: listening is off.
- **Blue**: push-to-talk mode, button not held. Press and hold it to speak.
- **Amber with "..."**: you hold the button. Wheelhouse asked to start listening and does not yet have an answer. The button turns purple if no answer arrives.
- **Purple with "!"**: the hold did not start listening, or listening stopped. The reason is in the button's tooltip, and Wheelhouse shows it as a notice as well.
- **Solid red**: listening is on.
- **Red that pulses to orange**: Wheelhouse receives speech.
- **A brief flash of green**: Wheelhouse processed the last utterance.
- **A small white dot in the lower-right corner**: Wheelhouse searches the window for a requested control. Wheelhouse draws the dot over the colour that shows at that time.

Available actions:

- **Click** to switch listening off, and click again to switch it on. In push-to-talk mode a click has no effect. That mode listens only during a hold.
- **Press and hold** for about a fifth of a second or longer to listen for the duration of the hold. On release, listening returns to the state it had before the hold. For example, in toggle mode with listening already on, listening stays on. This works in either interaction mode.
- **Double-click** to switch between toggle mode and push-to-talk mode.
- **Drag from the middle** to move the button. Wheelhouse saves its position and restores it at the next start.
- **Drag the outer edge** to resize it. It resizes around its own centre, so the point that you drag keeps its position relative to the middle. Wheelhouse saves both the size and the resulting position.
- **Hold Ctrl and roll the mouse wheel** over it to resize it. Both gestures use the same limits.
- **Right-click** to open the menu described below.

The resize area is a ring 8 pixels wide around the outer edge. The area inside that ring moves the button and starts push-to-talk. The pointer changes to a diagonal arrow over the ring. On a small button the ring narrows to a third of the radius, so that a movable centre always remains. The button size always stays between 15 and 150 pixels across. If a resize would place the button off the edge of the screen, Wheelhouse moves it back into view.

Wheelhouse checks that at least half of the button is on a screen. It checks at start-up and after a drag. It also checks after a monitor connects or disconnects or the display resolution changes. If less than half of the button is on a screen, Wheelhouse moves the whole button onto the nearest screen. Wheelhouse then saves that position. A button over the taskbar stays where it is. A button partly past an edge, with half of it still visible, also stays where it is.

To hide the button, right-click it and select "Show Floating Button" to remove the checkmark. The same menu item on the tray icon restores it. The button also hides by itself while a program has sole control of the speaker. It comes back when the program releases the speaker. This does not change the "Show Floating Button" setting. See [Plugins](#plugins).

### The tray icon

The tray icon is the Wheelhouse logo in the notification area near the clock. Its appearance does not change with the state of the program. The floating button reports state. The tray icon keeps Wheelhouse reachable when the floating button is hidden.

- **Left-click** to switch listening off and on, as with the floating button. In push-to-talk mode a left-click has no effect. The hold gesture works only on the floating button.
- **Double-click** to switch between toggle mode and push-to-talk mode.
- **Right-click** to open the menu below.

If the icon is absent, Wheelhouse did not start completely. See [Troubleshooting](#troubleshooting).

### The right-click menu

The floating button and the tray icon open the same menu. Most items are unavailable until Wheelhouse completes its start. "About Wheelhouse" is always available, because it depends on no other part of the program.

- **Speech Enabled** -- switch listening on or off. The checkmark shows the current state. In push-to-talk mode, when you select it, Wheelhouse switches listening on and changes the mode to toggle mode.
- **Show Floating Button** -- show or hide the floating button. The checkmark shows if it is visible.
- **Interim Results** -- this item selects one of two ways to type words. Wheelhouse types words as it recognizes them and corrects them afterwards, or it holds the words until the phrase ends. The first way is the streaming insertion described under [Speech Modes](#speech-modes). If you disable this item, you lose the immediate feedback and you get text that arrives already settled. Wheelhouse saves the choice in the settings file, so the next start uses it too.
- **Push-to-Talk Mode** -- switch between the two interaction modes. The checkmark shows when push-to-talk is active.
- **Listening on** -- shows the name of the microphone that Wheelhouse listens to, or "no microphone" when no microphone is open. You cannot select this item. It shows even before Wheelhouse completes its start.
- **Speech-to-Text Provider** -- select the speech engine: **Google Cloud** or **Parakeet V3 (Local)**. The list shows only the engines that are set up on this computer. It also shows **Parakeet V3 (Local) - model not installed** when the installer turned Parakeet off because its speech model is missing. See [Speech Engines](#speech-engines). The last item in this list, **Your training word...**, opens the window where you record the training word for the Parakeet engine. See [Your training word](#your-training-word).
- **AI Model** -- select the AI model. The list holds the model that the settings file names and, for a local AI server, the models that server offers. While the AI features are on, the model from the settings file always appears as a normal checked item. This is true even when the server no longer offers it. The menu does not report a missing model. When the AI features are off or no AI server is configured, the list holds the unavailable item "AI disabled" or "AI not configured". When the settings file names a model, a second unavailable line names it, for example "(configured: Gemma-4-E4B -- not available)".
- **Pattern Manager** -- open the editor for personal voice patterns. See [Voice Commands](#voice-commands).
- **Settings** -- open the Settings window. It holds the settings of this menu and seven more. Every control in it is a check box, a radio button, or a button, so "show numbers" and "click" with a number change any setting by voice. The spoken command "settings" opens the same window.
- **Debug** -- switch the detailed log on or off. Leave it off, except when you diagnose or report a problem.
- **Help** -- open the Wheelhouse Assistant in the browser. A short explanation window appears first. It has an **Assistant** button that opens the Assistant and a **Cancel** button that does not. To skip the window from then on, tick **Do not show this again** and then select **Assistant**. This is the same page, and the same window, that the spoken command "help" produces. See [Getting Help](#getting-help).
- **About Wheelhouse** -- open the About Wheelhouse window. It shows the program name and the version that runs. Include the version in any problem report. The window has a **Check for updates** button. The button starts a manual check of GitHub for a newer release. The spoken command "check for updates" opens the window and starts the same check. The result shows in the window. When a newer release exists, an **Update now** button appears. It works like the button in the update notice. A manual check also runs when `CHECK_FOR_UPDATES` is false and during the 7-day "Later" wait. It stores nothing. The window also shows a link to the Wheelhouse website, https://wheelhouse-project.org; a click on the link opens that address in the default browser. While a check runs, the button stays unavailable, so a second press sends no second request.
- **Restart Wheelhouse** -- stop the speech engine and the Wheelhouse processes, then start them again. The launcher process that started Wheelhouse continues to run and starts the new processes. This is the first step when speech recognition does not respond.
- **Exit** -- close Wheelhouse. You must do this before you run the installer to update and before you uninstall.

## Configuration

You do not need to edit any setting to use Wheelhouse. Every value ships with a working default. You can change the most common choices from the floating button's or the tray icon's right-click menu, with no file to open. These choices are which speech engine to use and push-to-talk or click-to-talk. You can also change the listening mode by voice. You cannot change the speech engine by voice.

The Settings window opens from the right-click menu or when you say "settings". It holds the settings of that menu and seven more. All other settings stay in config.toml. The **Help** button at the bottom of the Settings window opens the Wheelhouse Settings Help window, which explains each setting in the Settings window in the same order. This section covers the adjustments that you can make in the settings file.

**Where the settings file is.** Wheelhouse keeps its settings in a plain text file named config.toml, which opens in Notepad. It is at:

```
%LOCALAPPDATA%\Wheelhouse\app\services\wheelhouse\config.toml
```

To open the folder, press the Windows key and R together. Paste that folder path. Press Enter. The text `%LOCALAPPDATA%` expands to a folder that Windows hides by default, so paste the path and do not browse for the folder.

The installer creates config.toml from the template `config.toml.example` beside it. Your copy is personal to your machine. Nothing sends it anywhere.

Lines that start with a number sign are comments. The file documents many of its own settings inline. Wheelhouse removes those comments the first time that it saves a setting itself. This happens, for example, after you move or resize the floating button, or switch the listening mode or the speech engine. The template config.toml.example keeps them.

A few practical notes:

- Change one setting at a time. Then restart Wheelhouse, so the change takes effect.
- To restore the defaults, copy `config.toml.example` over `config.toml` in that same folder.
- The Sonos and Sony Bravia plugin sections have the headings `[plugins.sonos]` and `[plugins.bravia]`. These sections are off by default. Their comments say to enable them only if you own that hardware.

**The per-setting reference** -- the [configuration reference](https://wheelhouse-project.org/reference.html) lists every user-facing key that the shipped config.toml contains. It gives the default of each key and what the key does. The file leaves the three optional Sonos settings and the whole `[plugins.samsung]` section commented out. The [Plugins](#plugins) section describes them.

The file also leaves PATTERN_MANAGER_FONT_SIZE commented out. This setting is the font size, in points, of the Pattern Manager window. In that window, Ctrl+= makes the text larger, Ctrl+- makes it smaller, and Ctrl+0 returns it to the default size. Wheelhouse saves each change to this setting.

The allowed range is 7 to 24. Wheelhouse moves a value outside the range to the nearest end. The value must be a whole number without a decimal point. Wheelhouse ignores the value 12.0.

Two settings are important to know before you open the reference. The transcript log setting (LOG_TRANSCRIPTS) is off by default. This keeps dictated words and clipboard contents out of the log files. Enable it only when you diagnose a recognition problem. Then disable it again.

Wheelhouse never stores the API key of the AI server in config.toml. If your server requires a key, set the WHEELHOUSE_AI_API_KEY environment variable instead. This keeps the key out of a file that someone could copy or share.

**The setting that saves the sound the speech engine hears** (SAVE_ENGINE_AUDIO) is off by default. Use it to diagnose a first sound that is missing from a transcript. When you enable it, the Parakeet v3 speech engine saves one sound file for every phrase you say, in the folder `%LOCALAPPDATA%\WheelHouse\engine-audio`. While the Parakeet v3 engine runs, it deletes each of these files that is more than 2 days old. It does this when the engine starts, and once an hour after that.

While the engine does not run, the files stay. When you uninstall Wheelhouse, the uninstaller deletes the folder, even when you keep your personal data. The files hold everything you say, as sound, and they stay on this computer. A change takes effect the next time that the speech engine starts. An engine switch or a Wheelhouse restart starts it. Disable the setting again when you finish.

The rest of this section covers the most common adjustment: performance on slower hardware. For a setting that this section does not cover, the Wheelhouse Assistant answers questions about any key in the reference. See [Getting Help](#getting-help).

### Settings for slower hardware

If Wheelhouse responds slowly or is unreliable on an older computer, these changes help. They are in rough order of impact:

1. **Pick the right speech engine.** The default "parakeet_tdt" ([stt] last_provider) runs on any CPU. "google_stt" moves the work to the cloud. The cost is an account and an internet connection.
2. **Give yourself more time to speak.** Raise REPLACEMENT_TIMEOUT_MS and COMMAND_TIMEOUT_MS from 700 to 900-1000.
3. **Make text insertion slower.** Raise these settings under [ui_actions.timing]: post_paste_delay_ms (30 to 60), clipboard_operation_delay_ms (50 to 100), and clipboard_verification_timeout_ms (250 to 500). Do this if dictated text arrives incomplete or garbled.
4. **Give voice clicks more time.** Under [click], raise response_timeout_ms (3000 to 5000) and walk_deadline_ms (2500 to 4000) when clicks time out in complex windows. Keep walk_deadline_ms more than 250 below response_timeout_ms, so raise response_timeout_ms first. Otherwise Wheelhouse disables voice clicks, and the log names the setting. Raise screen_read_timeout_ms (10000 to 15000) if the numbered overlay says that it could not draw the numbers. This applies to windows that take several seconds to answer.
5. **Allow a local AI server longer to answer.** Raise [ai.server] timeout_s from 30 to 60 if corrections time out. You can also leave AI off. Nothing else depends on it.

## Plugins

Plugins are optional add-ons that connect Wheelhouse to extra hardware and services. These include your laptop screen, Sonos speakers, a Sony or Samsung TV, and a few Windows features. Each plugin described below has its own `[plugins.*]` section in config.toml with an `enabled` switch. With this switch, you can turn each plugin on or off without deleting anything. You do not need any of them for dictation and voice commands to work.

A plugin that cannot find its hardware at startup turns itself off for the session and reports the reason in the log. The rest of Wheelhouse runs unaffected. The Sonos and Samsung TV plugins are the exceptions. When a later check finds a TV speaker, the Sonos plugin turns itself on again without a restart. When the TV does not answer at startup, the Samsung TV plugin shows a notice and stays on. It tries the TV again at the first brightness command after 10 seconds.

Hardware that is present but unreachable is a different case. The Sonos plugin continues to poll a speaker whose address it resolved at startup. So if that speaker comes back on the network, the plugin finds it again without a restart.

Five of the plugins described below respond to the mouse thumb wheel. This is the small horizontal wheel on the side of the mouse, under your thumb. It is not the main scroll wheel. The main scroll wheel continues to scroll as normal.

Wheelhouse reads the thumb wheel directly from the device. This currently works only with Logitech MX-series mice.

Screen zones decide what the thumb wheel controls. If the pointer is at the left edge of the screen, the wheel adjusts brightness. Anywhere else, it adjusts volume. You do not need a command or a click. The configuration reference describes the settings for the step size and the zone width.

When no plugin controls the brightness of a screen, the brightness zone still works. For example, this happens on a desktop PC with an external monitor, where the Internal Panel plugin finds no panel and the TV plugins are off. If you turn the wheel down, it darkens the screen image in software. The setting `brightness_coordinator.software_dimmer` sets the method. If you turn the wheel up, it brightens the image again until no dimming is left. Wheelhouse removes this dimming when you exit it from the tray.

If a plugin controls a screen, the hardware changes first. This is true for a laptop screen, or for a TV plugin that is on. The software dimmer starts only when the hardware is as dark as it goes or the TV does not answer.

### Internal Panel

This plugin controls the brightness of a laptop's built-in screen from the brightness scroll zone. Enable or disable it with `plugins.internal_panel.enabled` (default: enabled). There are no other settings. Wheelhouse detects everything automatically. The plugin talks to the laptop display through a built-in Windows interface, entirely on your own machine. On a desktop PC with no built-in panel, it finds no display to control and stays inactive for the session.

### Sonos

This plugin adjusts Sonos speaker volume from the volume scroll zone. It also pauses Wheelhouse's listening while a music service plays on the Sonos, so that Wheelhouse does not transcribe song lyrics into your documents. Enable it with `plugins.sonos.enabled` (default: disabled).

Volume control through this plugin needs a specific arrangement. You need a Sonos sound bar that plays the audio of the display that this computer uses. Wheelhouse sends volume commands to one Sonos speaker, the TV speaker. It does this only when this plugin is enabled and Windows plays to an external audio device, not to the machine's own speakers. An output device with no name, for example while the TV is off, counts as external.

While the TV speaker that Wheelhouse identified earlier answers, it stays the TV speaker, whatever it plays. Otherwise, the TV speaker is the Sonos speaker that plays television audio. When no speaker plays television audio, the TV speaker is the speaker that Wheelhouse identified earlier. When Wheelhouse identified no speaker earlier, the TV speaker is the only Sonos sound bar on the network.

Another sound bar never replaces a TV speaker that stops answering. Wheelhouse finds a new TV sound bar only after a restart or when that sound bar plays television audio. Wheelhouse looks for speakers in every Sonos household on the network. If there are two or more sound bars, none plays television audio, and Wheelhouse identified none earlier, there is no TV speaker. Wheelhouse does not choose between two sound bars. When another sound bar starts to play television audio, Wheelhouse keeps the TV speaker it identified first while that speaker answers.

The volume scroll zone stays on the TV speaker whatever the TV speaker plays: television audio, music, a radio stream, or nothing. A change in what it plays never moves the volume commands. Volume commands go to the normal Windows volume instead in these cases:

- The plugin is disabled.
- The Windows output device is the machine's own speakers.
- No TV speaker exists.
- The TV speaker does not answer two checks in a row (about 90 seconds).

A check runs at startup, every 45 seconds, and when the thumb wheel turns while System Volume receives the volume commands. A volume command never waits for a check. Each time the choice changes, Wheelhouse writes one line to its log with the direction, the speaker name, and the reason. When the choice changes from System Volume to the TV speaker, Wheelhouse sets the Windows volume to 50 percent. When it changes to System Volume, the Windows volume stays as it is.

The listening pause that this section describes below also watches the TV speaker. The settings are:

- `polling_interval` sets how often to check if music plays, in seconds (default 2).
- `speaker_ip` is optional. A check runs at startup and every 45 seconds. The network search runs only when Wheelhouse has no record of a TV speaker or the TV speaker does not answer a direct query. Discovery runs only when this plugin is enabled and the Windows output device is an external one (a device with no name counts as external). Wheelhouse reads the name of the default output device. It skips discovery when that name looks like built-in hardware (Realtek, Intel, "Speakers", "Headphones", and similar). A speaker that discovery finds takes priority over this setting. Set this setting when discovery finds nothing.
- `request_connect_timeout` / `request_read_timeout` are network timeouts for advanced use (defaults 2.0 and 5.0 seconds). You rarely need to change them.

The plugin connects directly to the speaker over your home network. It uses no Sonos account and no internet service.

The pause that this plugin applies is narrow. It happens only when the Sonos plays from a music service. It does not happen when the Sonos plays audio that it received from this computer or from the television. That is deliberate, so that Wheelhouse keeps listening while you watch a film.

A second, separate mechanism can pause listening for computer audio, even if you do not own a Sonos. The setting `ENABLE_AUDIO_SUPPRESSION` controls it, and it takes three values. `true` pauses listening whenever sound plays, whatever Windows reports. `false` never pauses. With `"auto"`, the default, Wheelhouse asks Windows once at startup if the default microphone and the default communications microphone have an echo canceller. When Windows reports one on both, sound that this computer plays does not pause listening.

When Windows reports none on one of them, or cannot answer, Wheelhouse watches the sound level of the Windows output device. It pauses recognition while sound plays through that device. On a machine where Windows reports no echo canceller, sound from a screen reader also pauses listening.

A settings file from an older install has `true`. Change it to `"auto"` to let Windows decide. If you plug in a microphone or make it the default after startup, Wheelhouse checks it at the next start.

While the pause applies, the check runs on an interval. The interval widens to ten seconds while audio plays. Listening resumes by itself only after the sound stays quiet for about three seconds. So listening can take up to about thirteen seconds to resume after the audio stops.

The wake word ("computer") does not end this pause, because the sound itself can say the wake word. To turn listening on while the sound plays, click the floating button or the tray icon. In click-to-talk mode, that click ends the pause at once.

So audio that your computer plays can pause listening. This includes audio that it plays through a Sonos, when the Sonos is the Windows output device. The Windows output device never sees some audio, so this audio never pauses listening. This is audio that reaches the Sonos without passing through this computer: television audio over HDMI or an optical cable, and music that the Sonos streams by itself.

Some programs can take sole control of the speaker, so that no other sound plays through it. Kodi does this when audio passthrough is on, and other media players have a similar exclusive option. Sound from such a program does not reach the echo canceller or the sound-level check. So Wheelhouse pauses all listening while a program holds the speaker this way. During this pause Wheelhouse types nothing and runs no command. The wake word and push-to-talk do not end it.

Listening resumes by itself within about two seconds after the program releases the speaker.

The floating button hides while a program has sole control of the speaker. It comes back when the program lets go. It hides after the pause lasts one second. It comes back after the pause stays ended for one second. Because of these delays, a short hold does not make it blink.

This hide does not change the "Show Floating Button" setting. If you turn the button on during the pause, it shows at once. This pause follows the same setting as the sound-level pause above. It applies when that setting is `"auto"` or `true`. It does not apply when that setting is `false`.

### System Volume

This plugin controls the normal Windows volume (the same one as the taskbar speaker icon) from the volume scroll zone. It also quiets system audio while you hold the push-to-talk button. Enable it with `plugins.system_volume.enabled` (default: enabled). The settings are:

- `device_type` selects the audio device to control: `"default"` (the usual choice) or `"communications"`.
- `volume_step_db` sets the loudness change, in decibels, for each unit of `VOLUME_INCREMENT` (default 0.75). Each wheel step sends `VOLUME_INCREMENT` units (default 1). With both defaults, one wheel step changes the loudness by 0.75 dB.
- `min_volume_db` / `max_volume_db` set the volume floor and the volume ceiling (defaults -96.0 and 0.0).

This plugin works fully locally and uses no network. Both volume plugins can stay enabled. Wheelhouse picks one to receive volume commands. It picks Sonos when the Sonos plugin is enabled, the Windows output device is external, and a TV speaker exists (the Sonos section above explains how Wheelhouse finds it). It picks System Volume in every other case.

Wheelhouse picks again at startup, every 45 seconds, and when the thumb wheel turns while System Volume receives the volume commands. So the choice can change without a restart. When the choice changes from System Volume to the Sonos TV speaker, Wheelhouse sets the Windows volume to 50 percent. When it changes to System Volume, the Windows volume stays as it is. With `plugins.sonos.enabled` set to false, volume commands always go to System Volume, and Wheelhouse does not search the network for Sonos speakers.

### Bravia (Sony TV)

This plugin brings a Sony Bravia TV, used as a computer monitor, into Wheelhouse's brightness control. The brightness scroll zone can then dim and brighten the TV itself. Enable it with `plugins.bravia.enabled` (default: disabled). The settings are:

- `ip_address` is your TV's address on the home network. It is optional. If you leave it blank, Wheelhouse searches the network for the TV automatically. Set it if you have more than one TV or if discovery fails.
- `psk` is the pre-shared key that you set on the TV under Settings -> Network -> Home Network -> IP Control. It is a required setting. The plugin does not start with it blank.
- `device_name` is the TV's audio device name, exactly as Windows shows it under Sound settings -> Output (default "SONY TV"). It is not a label that you invent. Wheelhouse uses it to look up the device for spatial-sound handling. So it must match the Windows name exactly.

The plugin connects to the TV over your home network. It uses Sony's built-in remote-control interface. The plugin first checks if a Sony display has a physical connection. On a machine without one, it stays inactive for the session and issues no network requests.

### Samsung TV

This plugin brings a Samsung TV, used as a computer monitor, into Wheelhouse's brightness control. The brightness scroll zone can then dim and brighten the TV itself. Enable it with `plugins.samsung.enabled` (default: disabled). The plugin starts only when the value is exactly `true`.

The tests on real hardware covered only one model, the Samsung MRN65R95HAFXZA (R95H). Other Samsung TVs are likely to behave the same way, but the tests did not include them.

The plugin reaches the TV over your home network. So the TV must be on and reachable from this computer.

The plugin needs a saved pairing file before it can start, so the `[plugins.samsung]` section in `config.toml.example` is commented out. Pair the TV first (steps below). Then copy the section from `config.toml.example` into your config.toml and remove the leading number signs. The settings are:

- Set `enabled` to `true` to turn the plugin on.
- `ip_address` is the TV's address on your home network, a private IPv4 address such as 192.168.1.50. It is a required setting. Wheelhouse does not search the network for a Samsung TV. Use the address that you paired with. Wheelhouse refuses a pairing that you saved for a different address.
- `credential_file` is the full path of the pairing file that the pairing step saves. Write it with forward slashes inside quotes. It is a required setting. The plugin does not start with it blank.

Leave `plugins.bravia.enabled` at its default (disabled) when you use the Samsung plugin.

To pair the TV (once per TV), first do two things:

1. On the TV, enable IP Remote in the network expert settings. Note the TV's IP address.
2. Create a folder for the pairing file outside the Wheelhouse application folder. An update replaces the application folder. So an update deletes a file that you keep inside it.

Then run the pairing script that ships with Wheelhouse. Use the Wheelhouse Python environment. If you installed Wheelhouse with the installer, the application folder is `%LOCALAPPDATA%\Wheelhouse\app`. In PowerShell, run this command as one line:

```
& "$env:LOCALAPPDATA\Wheelhouse\app\services\wheelhouse\.venv\Scripts\python.exe" "$env:LOCALAPPDATA\Wheelhouse\app\scripts\pair_samsung_tv.py" --ip-address 192.168.1.50 --credential-file C:\Users\you\WheelhousePairing\samsung-tv.dat --expected-model MRN65R95HAFXZA
```

`--ip-address` is the TV's address. `--credential-file` is a full path to a new file in the folder from step 2. `--expected-model` is the TV's exact model name. The script stops with `identity_mismatch` when the TV reports a different one. The TV reports its model name in the `modelName` field of the page at `http://<TV address>:8001/api/v2/`. When the pairing prompt appears on the TV, choose Allow.

The script saves the pairing file with Windows encryption. It prints the TV's current brightness and changes no picture setting. Only the Windows account that paired can use the file. The script refuses to overwrite a file that exists. To pair again, give it a new file name and update `credential_file`. Restart Wheelhouse after you edit config.toml.

Wheelhouse's 0 to 100 brightness scale maps onto the TV's own 0 to 50 backlight range. Wheelhouse reads each change back from the TV.

If the TV is off or does not answer in time, Wheelhouse shows a notice. The software dimmer then takes the dim step. For the next 10 seconds, Wheelhouse does not contact the TV and does not wait for it. Each brightness command goes the same way as a command that the TV did not answer. The first brightness command after the 10 seconds tries the TV again.

The notice also says that the TV's address possibly changed, for example after a router restart. Reserve the address on the router. Alternatively, put the new address in `ip_address` and pair the TV again. A pairing that is missing, that the TV refused, or that no longer matches the TV also produces a notice. If that happens, pair the TV again.

### Idle Monitor

This plugin notices when you step away (no keyboard or mouse activity). It pauses listening, so that Wheelhouse does not transcribe an empty room. Listening resumes when you return or say the wake word. In push-to-talk mode the wake word ends only the idle pause. Listening then waits for your next hold of the floating button.

Enable it with `plugins.idle_monitor.enabled` (default: enabled). The settings are `idle_timeout_minutes` (default 10) and `polling_interval_seconds` (default 4). The plugin is fully local. It only asks Windows how long ago your last keypress or mouse move happened. The measure is keyboard and mouse activity, not sound. If you watch a film without touching either, listening pauses once the timeout passes.

### Window Positioning

This plugin automatically moves the Windows On-Screen Keyboard out of the way when it would cover the window that you work in. Enable it with `plugins.window_positioning.enabled` (default: enabled). The settings are as follows.

`target_window_names` lists the windows to move (default: the On-Screen Keyboard). `move_cooldown_seconds` (default 0.5) prevents jitter. `clearance_gap_pixels` has the default 5. `ignore_window_titles` and `ignore_window_classes` name windows that never trigger a move.

The plugin is fully local.

**Administrator rights.** Windows does not allow a program to move the window of a program that runs at a higher privilege level.

If the on-screen keyboard runs as administrator and Wheelhouse does not, the keyboard does not move. Wheelhouse shows nothing on screen. It records the reason only in the debug log. To make the move work in that case, run Wheelhouse as administrator as well. This applies only when the keyboard itself starts with administrator rights. Windows does not normally start it this way.

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

- Confirm the plugin's `enabled = true`. Then restart Wheelhouse. Wheelhouse discovers plugins only at startup.
- Check the log's startup lines: each plugin reports if it initialized, went inactive (hardware not found), or failed, usually with the reason.
- For Sonos and Bravia, make sure that the device is on and reachable from this PC on the same network.
- For Bravia, enable IP Control on the TV. The pre-shared key in config.toml must match the key that you set on the TV.
- If the thumb wheel does nothing, check the screen zones. If the pointer is at the left edge of the screen, the wheel adjusts brightness. Anywhere else, it adjusts volume. Volume needs at least one volume plugin that is enabled. Brightness needs no plugin. Without one, it uses the software dimmer.

## Troubleshooting

Start with the verification checks below. The Wheelhouse Assistant can also read an error message or a log excerpt and identify the cause. See [Getting Help](#getting-help).

### Setup verification

Run these five checks in order. Stop at the first that fails and read the entry it names.

1. **Did the installer finish without error lines?** If not, see [Installer troubleshooting](wheelhouse_install.md#installer-troubleshooting) in the installation guide.
2. **Do Windows Sound settings show the microphone receiving sound?** Right-click the speaker icon on the taskbar, open Sound settings, open Input, and speak. If the input meter does not move, see "Microphone not detected."
3. **Is the Wheelhouse icon present in the system tray?** The tray icon is the check that matters. You can disable the floating button from the menu on the floating button or the tray icon. The floating button also hides while a program has sole control of the speaker. For these reasons, its absence does not mean that the program failed to start. If the tray icon is missing, see "Wheelhouse does not start and neither the tray icon nor the floating button appears."
4. **Open Notepad, click in the empty page, and say "hello".** If the word does not appear, see "Dictation not appearing in text fields."
5. **Say "undo".** If the word does not disappear, see "Commands not recognized."

If all five checks pass, the installation works. Any remaining problem is specific to one application or feature. The entries below cover the common cases.

### Common Problems

**Microphone not detected**

- *Symptom:* Wheelhouse starts, nothing happens when you speak, and Windows Sound settings show no input activity.
- *Likely cause:* Windows uses a different microphone, or a privacy setting blocks desktop applications from the microphone.
- *Action:* Open Settings > Privacy and security > Microphone and confirm that "Let desktop apps access your microphone" is on. Then open Sound settings > Input and select the microphone that you use. Restart Wheelhouse afterward.
- *Note:* When the privacy setting blocks the microphone, Wheelhouse shows a notice titled "Windows is blocking your microphone". The notice names the same setting. It stays on the screen until you close it.
- *Check:* The menu on the floating button or the tray icon shows "Listening on:" and the name of the microphone that Wheelhouse uses. "Listening on: no microphone" means that no speech engine has a microphone open.

**Wheelhouse does not start and neither the tray icon nor the floating button appears**

- *Symptom:* You start Wheelhouse and nothing appears. Judge by the tray icon. The floating button is hidden when "Show Floating Button" is off. It is also hidden while a program has sole control of the speaker.
- *Likely cause:* One of the background processes failed during startup, most often because a speech model is missing or an earlier install stopped before it finished.
- *Action:* Re-run the installer. It repairs a broken install and keeps your settings and personal data. If Wheelhouse still does not start, restart the computer and try once more before you file a report.

**Speech engine not connecting**

- *Symptom:* Wheelhouse reports that the speech engine is disconnected, or it stays in a waiting state and does not recognize speech. A window can say that loading the engine takes longer than usual and that Wheelhouse cannot hear you yet. The window stays until the engine is ready. If the engine stops, or is not ready at the end of a time limit, the window closes and a notice appears. At the end of the time limit, the notice says that the engine did not start and tells you to choose Restart Wheelhouse in the tray menu.
- *Likely cause:* The speech engine failed to start. Common reasons: its model was never downloaded, the Google Cloud engine has no credentials, or the computer is low on memory.
- *Action:* Switch engines from the menu on the floating button or the tray icon. Parakeet, the built-in offline engine, needs no account. If you never installed the engine that you need, re-run the installer and select it at the engine question. For Google Cloud, set the path of the key file in the settings file. Or set the GOOGLE_APPLICATION_CREDENTIALS environment variable. Wheelhouse reads the variable only when the settings file names no key. See [Speech Engines](#speech-engines). If the engine will not start right after an install or an update, run "uv --version" in a new PowerShell window. If the answer is that uv is not found, the tools of the installer are not on the PATH. Re-run the installer to correct that.

**Speech will not start and a notice names the Microsoft Visual C++ runtime**

- *Symptom:* Wheelhouse starts, but speech never becomes ready, and a notice says speech needs a newer Microsoft Visual C++ runtime.
- *Likely cause:* The speech engine's native files need the Microsoft Visual C++ runtime. That runtime is not part of Windows, so a computer that never installed it, or installed an old copy, cannot load them. The speech process then stops within a few seconds of starting, and no text ever appears.
- *Action:* Run the Wheelhouse installer again and select Yes when Windows asks for permission. The installer then installs the runtime for you. The installer also runs its other steps again. It replaces Wheelhouse with the release that the installer provides, which can be newer than the release that you have. It keeps your settings and voice patterns. The wizard also pre-ticks "Start Wheelhouse automatically when I log in" and "Start Wheelhouse now, when setup finishes". Clear each box that you do not want. If you already ran the installer again and the speech still does not start, restart the computer. Sometimes the Microsoft installer needs a restart before Windows uses the new runtime. If you cannot use the installer, for example because your account cannot approve the Windows permission prompt, ask a person with an administrator account to install the Microsoft Visual C++ Redistributable (x64) from Microsoft's own page at https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist, under the heading "Visual C++ v14 Redistributable". Start Wheelhouse again afterward.

**Commands not recognized**

- *Symptom:* A command such as "maximize" has no effect, or Wheelhouse types it as text.
- *Likely cause:* The speech engine returned a different word, for example "maximum" instead of "maximize". Or the utterance overlapped with audio that played at the same time.
- *Action:* Speak the command as a separate utterance, with a brief pause before it. Use normal conversational volume, because a raised volume reduces accuracy. If the engine misunderstands one word many times, select a copy of the word with the correct spelling and say "boost". See its entry in [Voice Commands](#voice-commands). Only Google applies hints as shipped. Parakeet applies them only when hint biasing is on. Otherwise Wheelhouse types "boost" as a word.

**Command words are typed as text instead of running**

- *Symptom:* The window stays open, and "close window" is typed into the document.
- *Likely cause:* Expected behavior. The safety word protects commands that would be disruptive or hard to undo if they ran while you dictated. These commands require "x-ray" first.
- *Action:* Say "x-ray close window". The Voice Commands section of this document marks which commands require the safety word.
- *Note:* The reverse also happens. Wheelhouse types a command that needs no safety word, such as "click [name]" or "activate [app name]", as text when it finds no control, window, or program with that name. If you say "x-ray" before the command, Wheelhouse shows a notice that explains the failure and types nothing.

**Dictation not appearing in text fields**

- *Symptom:* Wheelhouse recognizes the speech but no text appears in the application.
- *Likely cause:* The text field does not have the focus, or Wheelhouse could not confirm that the focused control accepts text. Wheelhouse then pastes your words with Ctrl+V and does not type them. The words arrive only where a paste works. A web browser page that is not a text field takes no paste. Words that you speak while the page itself has the focus arrive nowhere.
- *Action:* Click inside the text field and try again. If the application runs as administrator, Wheelhouse cannot reach it at all and shows a notice that says so. Use the physical keyboard there, or run Wheelhouse as administrator as well. If dictation works in Notepad, the problem is specific to the text field of that application.

**Words from a playing video are typed, or listening pauses whenever sound plays**

- *Symptom:* Wheelhouse types the words of a video or other audio that plays, or listening pauses every time the computer plays sound.
- *Likely cause:* The microphone's "Audio enhancements" setting in Windows is Off. With that value, Windows offers no echo canceller for the microphone. The microphone then also hears what the speakers play.
- *Action:* Open Settings > System > Sound > Input, select the microphone, and set "Audio enhancements" to "Device Default Effects" or "Voice Clarity". Either value gives the microphone the Windows echo canceller again. Then restart Wheelhouse. Wheelhouse asks Windows about the echo canceller only once, at start.
- *Check:* After the restart, the log file wheelhouse.log contains the line "Audio suppression off: Windows reports an echo canceller", followed by the microphone's name.

**AI text correction does nothing or times out**

- *Symptom:* Wheelhouse does not correct the selected text although AI is enabled. Text correction and rewriting are the built-in AI commands. A personal pattern can also send its own prompt to the AI with the **Ask AI** action, which the Pattern Manager lists under Advanced actions. The in-app help chat is currently disabled.
- *Likely cause:* Wheelhouse sends requests to an AI server and does not run the model inside itself. The server is either one that you point Wheelhouse at, or one that Wheelhouse starts on this machine. It starts one when [ai.runtime] enabled is true and you ran the command-line installer with `-AiMode local`. To find if the server is reachable, Wheelhouse asks the server for its model list. If that server is absent or unreachable, the AI commands show "AI is not available right now." and change nothing. The rest of the program continues to run. A server that answers but lacks the configured model does not switch AI off. Each request shows "The AI server doesn't have the configured model. Check the model name in the AI settings. Original text preserved." A server too slow to answer within the time limit makes the request fail. "x-ray fix" then shows "Correction failed. Original text preserved." The five rewrite commands ("simplify", "shorten", "x-ray make formal", "pirate", and "x-ray translate to [language]") show "Rewrite failed. Original text preserved." When the server has also stopped answering the check, all six commands show "The AI server isn't responding. Original text preserved." in place of those texts.
- *Action, in order:* Confirm that [ai] enabled = true and that [ai.server] base_url is set. An empty base_url disables AI by design. Confirm that the server is reachable at that address. Confirm that the [ai.server] model name is one that the server provides. If the server is slow to respond, raise [ai.server] timeout_s. If a remote server needs a key, set the WHEELHOUSE_AI_API_KEY environment variable. Wheelhouse never stores the key in the settings file. Restart Wheelhouse.
- *Note:* An unreachable AI server does not affect dictation, voice commands, or any other feature.

---

## Getting Help

**The Wheelhouse Assistant is the first place to ask.** It holds this entire document, the reference for actions, notices, and configuration settings, and the project's own notes on how each part behaves. It answers questions in plain language. It reads an error message or a log excerpt and identifies the likely cause. It names the specific setting to change and the file that the setting belongs in. It covers material that this document summarizes, so it can answer questions that no section here addresses.

<https://notebook.google.com/notebook/da51a404-67ec-4804-9ebe-83605df3e9cf/preview>

Three ways to reach it: the address above, the **Help** item in the right-click menu on the floating button or the tray icon, and the spoken command "help". All three open the same assistant in the default browser.

The Wheelhouse Assistant runs on Google's Gemini Notebook, so you must sign in with a Google Account. The account is free, and Google does not ask for a credit card. You can use an email address you already have; a Gmail address is not necessary. To create an account, click "Create account" on the Google sign-in page. If you are signed in with a work or school account and the Assistant does not open, sign in with a personal account instead.

The menu item and the spoken command first show a short explanation window. Its heading is **Ask the Wheelhouse Assistant**. The window says what the assistant is and repeats the Google Account text above. The **Assistant** button opens the assistant. The **Cancel** button or the Escape key closes the window and opens nothing. If you tick **Do not show this again** and then select **Assistant**, Help opens the browser directly from then on.

The window appears at every Help, with the box already ticked, while both of these conditions are true. `explain_before_open` is false. The file help_explainer_notebook_shown.toml is missing from the data folder next to the settings file. Only the **Assistant** button writes that file. After **Cancel**, the window appears again at the next Help. To keep the window off, select **Assistant** with the box still ticked.

If Windows cannot start a browser, Wheelhouse shows the notice "Wheelhouse could not open your browser." If the notice says "Online help is not configured. Set assistant_url under [ai.help].", the settings file holds no assistant address. In that case, set assistant_url under [ai.help], then restart Wheelhouse. Release 1.2.1 and earlier named the setting gem_url. When you update with the installer, it renames the setting to assistant_url and replaces an old assistant address with the current one.

To show the window again, set `explain_before_open = true` under `[ai.help]` in the settings file. See [Configuration](#configuration).

The assistant cannot access your computer. It cannot read a log unless you paste the log into it. It does not know about changes that the project made after the release that the assistant uses as its source.

**How to report a defect.** Report a problem in Wheelhouse itself to the project. Such a problem is a behavior that is incorrect, not something that needs an explanation. Use one of these methods:

- Open an issue or start a discussion at https://github.com/wheelhouse-project/Wheelhouse.
- Or email `help@wheelhouse-project.org`.

Include these items: the Wheelhouse version from **About Wheelhouse** in the right-click menu, what you did, what you expected, and what happened instead. Paste any error message in full. If the installer failed, attach the setup log of the installer. Installer messages and log lines contain no dictated text, unless you turned on the setting that logs transcripts. See [Configuration](#configuration).

---

Generated: 2026-11-15 for the v1.4.4 release
Wheelhouse version: 1.4.4
