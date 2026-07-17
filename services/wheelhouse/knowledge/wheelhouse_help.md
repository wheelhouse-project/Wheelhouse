# WheelHouse Help Document

## Instructions for AI Assistant

You are a friendly, patient WheelHouse support assistant. Your job is to help
users understand and use WheelHouse, a voice-controlled desktop automation
system for Windows.

Behavior rules:
- Match your depth to the question. Simple question = simple answer. Technical
  question = technical answer.
- If the user seems non-technical, avoid jargon. Use analogies.
- If unsure whether the user wants a quick or detailed answer, ask:
  "Would you like a quick answer or a deeper explanation?"
- For WheelHouse-specific questions: answer ONLY from this document. Never
  invent features, commands, or settings not documented here.
- For general computing questions (microphone setup, Windows settings,
  PowerShell basics): help freely using your general knowledge.
- If the answer isn't in this document: "I don't have information about that
  feature. You can reach the developer at the WheelHouse GitHub page: https://github.com/wheelhouse-project/WheelHouse (open an issue or start a discussion)."
- When describing voice commands, always give an example of what to say.
- When a user seems overwhelmed, direct them to the "Day 1 Quick Start" section
  and tell them to ignore everything else until they're comfortable.
- When a user asks about hardware or performance, be honest about limitations.
  Don't promise it will work on every machine.
- Greet the user and ask what they need help with.

---

## What Is WheelHouse

WheelHouse gives you hands-free control of your Windows PC by voice. You speak, and your computer responds: dictate text into any application, issue commands, switch windows, launch programs, and click on-screen buttons by name -- all without touching the keyboard or mouse. If you use a mouse with a thumb wheel, WheelHouse can also put screen brightness and volume on that wheel.

**Who is it for?** WheelHouse is built first for people who need it: if using a keyboard and mouse is painful, difficult, or impossible, WheelHouse aims to give you the whole computer by voice. It is equally at home with anyone who simply wants a faster, more natural, hands-free way to work.

**What do you need?**
- A Windows 10 or Windows 11 PC (64-bit)
- A microphone (a headset or external microphone usually hears you better than a built-in laptop microphone, but start with whatever you have)
- About 10 GB of free disk space (the speech model is the biggest part)

That is the whole list. The installer brings everything else WheelHouse needs, checks your hardware before it starts, and asks nothing of you afterward: no account to create, no subscription, and nothing extra to install first.

**Will my voice leave my computer?** No -- not unless you choose that. Out of the box, WheelHouse turns your speech into text entirely on your own machine. No audio or text is sent anywhere, and there is no telemetry. Cloud speech recognition exists only as an option you would have to deliberately turn on.

**How it works, in one paragraph.** You speak into your microphone. WheelHouse turns your words into text on your own computer, then decides what you meant. If it sounds like a command ("undo", "select all"), WheelHouse carries it out immediately. Otherwise it treats your words as dictation and types them into whatever window you are working in -- a document, an email, a chat box -- with capitalization and spacing handled automatically. Punctuation is spoken: say "comma" or "period" and the symbol appears. Words show up as you talk, usually starting within about two seconds, and keep flowing while you speak instead of arriving all at once after you stop.

**An honest note.** WheelHouse is a young open-source project with a single primary author. Reliability is its first value and it is the author's daily driver, but it has been tested on a limited set of machines so far, so you may meet rough edges on hardware or applications it has not seen. If something fails for you, that report is genuinely wanted: https://github.com/wheelhouse-project/WheelHouse

---

## Day 1 Quick Start

**Stop here if you are new. Do these steps first, and ignore the rest of this document until they work.**

1. Open PowerShell (press the Windows key, type "powershell", press Enter) and run this one line:
   ```
   irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
   ```
   The installer downloads the speech model, so give it time. When it asks a question, pressing Enter accepts the default.
2. Start WheelHouse from the Start menu or the desktop shortcut (the installer creates both).
3. Open Notepad.
4. Say **"hello world"** -- the words "hello world" appear.
5. Say **"new line"** -- the cursor moves to a new line.
6. Say **"undo"** -- the text is undone.
7. Say **"select all"** -- the text is highlighted.

**That's it -- you are using WheelHouse.** Ignore everything else in this document until you feel comfortable with these basics.

One thing worth knowing on day 1: WheelHouse starts in click-to-talk (toggle) mode -- click once to start listening, click again to stop. If you would rather hold a button down while you speak, see the "Interaction Modes" section.

---

## Pick Your Path

Once the basics work, pick the path that matches what you want to do next.

- **"I just want to dictate text into emails, documents, and chat."** Go to: Voice Commands (especially the dictation and punctuation subsections), then Speech Modes.
- **"I'm a programmer or power user and want everything WheelHouse can do."** Go to: the full Voice Commands reference (commands, formatting, and navigation), then the Configuration section.
- **"I'm setting things up, or something isn't working."** Go to: Getting Started, then Configuration, then Troubleshooting.

---

## Getting Started (Full Version)

### Installation

You do not need to install anything ahead of time -- no programming tools, no separate downloads. One command does the whole job. Open any PowerShell window (press the Windows key, type "powershell", press Enter) and run:

```
irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

The whole process takes about 10 to 20 minutes, most of it downloading (roughly 1 GB in total). In plain language, the installer:

1. Checks that your computer meets the requirements (see below) and tells you clearly if something is missing.
2. Installs uv, the environment manager WheelHouse uses. Nothing is installed system-wide; everything lives inside WheelHouse's own folder.
3. Downloads the WheelHouse application and verifies the download is genuine and undamaged.
4. Sets up WheelHouse's own private Python environments -- these are self-contained and cannot interfere with anything else on your computer.
5. Asks which speech engine you want (the default answer is right for almost everyone -- see Speech Engines and Accounts below).
6. Downloads the offline speech model if you chose the default engine (about 650 MB -- this is the longest step).
7. Creates Start-menu and desktop shortcuts, then asks two final questions: whether WheelHouse should start automatically when you log in (for hands-free use, answering yes is strongly recommended), and whether to start WheelHouse right now.

WheelHouse installs for your user account only. No administrator rights are needed, and it does not touch other programs on your computer.

### What you need

- Windows 10 or 11, 64-bit (Windows 11 any edition; most Windows 10 editions work too)
- 10 GB of free disk space
- 8 GB of memory (RAM) -- this is a hard minimum for the built-in offline speech engine; 16 GB is recommended. Below 8 GB the installer stops and suggests the Google Cloud engine, which runs in the cloud and needs far less memory.
- 4 or more CPU cores recommended -- with fewer, WheelHouse still installs, but speech recognition may respond slowly
- A microphone (you can plug one in after installing)
- An internet connection for the install itself; the default speech engine works fully offline after that

### What successful installation looks like

The installer reports each step as it goes. If it reached the speech-engine question, finished its downloads, created your shortcuts, and asked the two final questions (start at login? start now?) without stopping on an error, you are done. You will find WheelHouse in the Start menu under W and as a desktop shortcut.

### What failure looks like

Every failure message the installer prints is designed to be understandable and safe to share. The common ones:

- **"WheelHouse appears to be running"** (during an update): the installer refuses to replace an app that is running. Exit WheelHouse first -- right-click the WheelHouse tray icon and choose Exit -- then run the installer again. If it says it could not even check whether WheelHouse is running, close WheelHouse or restart the computer, then try again.
- **"This computer has N GB of memory"**: your machine is below the 8 GB minimum for the offline speech engine. Choose the Google Cloud engine instead (it needs far less memory), or add memory.
- **"Not enough free disk space"**: free up 10 GB on the Windows drive and run the installer again.
- **"tar.exe was not found"**: only affects Windows 10 versions from before 2018, which lack the tool that unpacks the speech model. Install tar yourself, or choose the Google Cloud engine (which needs no model download).
- **"Could not install uv"**: usually a blocked network -- corporate proxies can block the download. Install uv manually from https://docs.astral.sh/uv/getting-started/installation/ and run the installer again.
- **"... failed its integrity check"**: the downloaded file does not match its published fingerprint. An antivirus or proxy rewriting downloads is the most common cause; add an exception or try a different network.
- **"Downloading ... failed twice"**: network trouble. Run the installer again -- downloads resume where they left off.

**Re-running the installer is always safe.** It repairs a broken install, resumes interrupted downloads, and updates an existing install while preserving your settings, your personal voice patterns, and the downloaded speech model. You cannot make things worse by running it again -- when in doubt, re-run it.

If none of that helps, ask for help at https://github.com/wheelhouse-project/WheelHouse -- paste the installer's output into your report.

### First run

When you start WheelHouse, several separate programs come up together as a team:

- **The launcher** -- the piece you actually started. It supervises the others and restarts them if one crashes.
- **The logic process** -- the brain. It decides what your speech means and routes it to the right action.
- **The input process** -- the fingers. It types text, presses keys, and clicks for you.
- **The GUI process** -- the WheelHouse icon in the system tray and the small floating status button.
- **The speech engine** -- runs as its own helper program, turning your voice into text.

Within a few seconds you should see the WheelHouse icon in the system tray (the area near the clock). If it does not appear, see Troubleshooting.

### Microphone verification

Before judging WheelHouse, make sure Windows itself can hear your microphone. Three quick checks, in order:

1. **The privacy setting first.** Open Settings > Privacy and security > Microphone, and make sure "Let desktop apps access your microphone" is on. This one switch silently blocks everything if it is off.
2. **The input meter.** Right-click the speaker icon in your taskbar, choose Sound settings, and scroll to Input. Your microphone should be selected, and when you speak, the level meter should bounce. If it stays flat, pick a different input device or plug in a different microphone.
3. **The Notepad test.** Open Notepad, make sure WheelHouse is listening, and say "hello world". On a modern computer the words should appear within about two seconds.

### The hotword ("x-ray")

Some commands could do real damage if they fired by accident while you were dictating a sentence -- closing a window, for example. WheelHouse protects those commands with a hotword: they only run when the utterance starts with the word "x-ray".

- Say "close window" -> nothing dangerous happens; the words are treated as ordinary dictation.
- Say "x-ray close window" -> the active window closes.

Harmless everyday commands like "undo", "copy", and "select all" do not need the hotword. The Voice Commands section marks the commands that require it.

### The wake word ("computer")

If you are quiet for a while, WheelHouse can pause its listening to save effort. Saying "computer" wakes it back up -- no keyboard or mouse needed, which matters if you rely on hands-free control. The wake word and the hotword are different things: "computer" resumes listening after an idle pause, while "x-ray" unlocks protected commands. Wake-word behavior can be tuned in the settings file (the wake_word section); it is on by default.

---

## Speech Engines and Accounts

### Do I need a Google account? (Short answer: probably not)

Most users need no account of any kind. WheelHouse ships with the **Parakeet** engine as its default: it runs entirely on your own computer, on the regular processor (CPU), works offline, costs nothing, and never sends your audio anywhere. The installer downloads its model for you, and it is preselected in your settings from the start.

The one situation where an account comes up: a computer with less than 8 GB of memory cannot run the offline engine usefully, so the installer suggests the **Google Cloud** engine instead. That engine processes your speech on Google's servers and needs a free Google Cloud account plus a one-time credentials setup (Google charges for heavy use beyond its free tier, but most personal use stays within it). If your machine meets the 8 GB minimum, you can ignore Google entirely.

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

The Google Cloud engine cannot hear you until it has credentials. This is the one engine that requires technical setup:

1. Create a Google Cloud account and a project at https://console.cloud.google.com/.
2. In the project, enable the Cloud Speech-to-Text API.
3. Create a service account (under IAM & Admin > Service Accounts) and give it the Cloud Speech Client role.
4. Create a JSON key for that service account; a small file downloads.
5. Move the file somewhere permanent on your computer.
6. Press the Windows key, type "environment variables", open "Edit environment variables for your account", and add a new variable named GOOGLE_APPLICATION_CREDENTIALS whose value is the full path to that file.
7. Restart WheelHouse if it is running.

### Switching engines

The easiest way: right-click the WheelHouse icon in the system tray, open **STT Provider**, and pick the engine you want. WheelHouse remembers your choice (it is stored as last_provider in the stt section of the settings file) and uses it the next time it starts.

If the engine you want was never set up on this machine -- for example you originally chose Google Cloud and now want Parakeet -- re-run the installer and pick the engine at its speech-engine question; it downloads whatever that engine needs.

---

## Can My Computer Handle WheelHouse?

An honest answer, because nothing is worse than installing something and finding it unusable.

### Minimum

- Windows 10 or Windows 11, 64-bit
- A dual-core processor -- WheelHouse will install and run, but speech recognition may respond slowly; 4 or more cores is the comfortable floor
- 8 GB of RAM -- a hard minimum for the built-in offline speech engine; below it, the installer stops and points you to the cloud engine instead
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

- **If your computer runs Chrome with a handful of tabs and a Zoom call at the same time without struggling, WheelHouse should work fine.** That is the practical baseline.
- If your computer already feels sluggish at basic tasks -- slow window switching, laggy typing in the browser -- expect noticeable delays in WheelHouse too. It will still work; it will just feel slow.
- The honest test is simply to try it: installation is free, safe to re-run, and easy to uninstall. If dictated words regularly take 3-4 seconds or more to appear, switch to a different engine (see Speech Engines and Accounts) rather than giving up.

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

WheelHouse turns what you say into keystrokes, text, and system actions. Most commands work without any prefix, but some powerful or destructive commands require you to start with the hotword **"x-ray"** so they cannot fire accidentally while you are dictating normal text. Commands that need the hotword are shown with an "x-ray" prefix in the tables below; everything else works bare.

Two kinds of voice patterns exist, and it helps to know the difference:

- **Commands** do something: press a key, switch a window, click a button. Most must be spoken as their own utterance (say the command, then pause).
- **Replacements** work **inline during dictation**: you say them in the middle of a sentence and WheelHouse swaps the spoken word for a symbol or corrected text as it types. All of the punctuation words ("period", "comma", "question mark") work this way -- you never have to stop dictating to punctuate.

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

These commands control what WheelHouse types and give you escape hatches when a word you want to dictate collides with a command.

| Say this | What happens | Notes |
|---|---|---|
| literal [words] | Types the words after "literal" exactly, skipping all command and replacement processing | The escape hatch -- see the detailed explanation in "Special Commands" below |
| insert [text] | Inserts raw text with no capitalization, spacing, or formatting applied | Useful for exact fragments like an email address or a product code |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item 1", "item 5" |
| submit | Presses Enter | Also works as the last word of a sentence: "hello world submit" types "hello world" and then presses Enter. To type the word itself, say "literal submit" |

One background protection worth knowing about: utterances that begin with "okay Google", "ok Google", or "hey Google" are silently discarded. If you talk to a nearby voice assistant while WheelHouse is listening, that cross-talk is not typed into your document.

#### Text Editing

**Deleting and correcting**

| Say this | What happens | Notes |
|---|---|---|
| backspace | Deletes one character to the left | |
| backspace [number] | Deletes that many characters to the left | e.g. "backspace 5"; counts are capped at 50 |
| delete | Deletes one character to the right | |
| delete [number] | Deletes that many characters to the right | e.g. "delete 5"; counts are capped at 50 |
| delete word | Deletes the entire word under the cursor | |
| undo | Undoes the last action (Ctrl+Z) | |
| undo [number] | Undoes multiple steps | e.g. "undo 3" |
| redo | Redoes the last undone action (Ctrl+Y) | |
| redo [number] | Redoes multiple steps | |

WheelHouse also accepts the common ways the speech engine misspells "undo" and "redo" (heard as "undue", "undu", or "redu"), so the command still fires even when the recognizer gets the spelling wrong.

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
| copy | Copies the current selection | |
| copy line | Copies the entire current line | |
| copy all | Selects everything and copies it | |
| copy screen | Starts the Windows screenshot snipping tool | |
| x-ray cut | Cuts the current selection | Requires the hotword for safety |
| paste | Pastes the clipboard contents | |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires the hotword |

**Selection**

| Say this | What happens |
|---|---|
| select all | Selects everything in the current field |
| select word | Selects the word under the cursor |
| select line | Selects the current line |
| select paragraph | Selects the current paragraph |

**Saving, finding, and searching**

| Say this | What happens | Notes |
|---|---|---|
| x-ray save | Saves the current document (Ctrl+S) | |
| x-ray find [text] | Opens the app's find bar and types the search term | e.g. "x-ray find invoice" |
| x-ray replace | Opens find-and-replace (Ctrl+H) | |
| x-ray search | Copies the current selection and runs a web search for it | Select the text first |

##### The "press [keys]" Command in Detail

"press [keys]" is the generic escape hatch for any keyboard shortcut. Modifiers are automatically held down first regardless of the order you say them -- so "press delete control" is equivalent to "press control delete". If any word in the phrase is unrecognized, WheelHouse presses nothing and discards the phrase; it is not typed as text. If your speech engine hyphenates a token (hearing "f-11" or "control-alt"), WheelHouse untangles that automatically.

**Modifier keys you can say**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: f1 through f12.

**Letters**: any single letter a through z. Example: "press control shift t".

**Digits**: a digit works only when another key name follows it. Avoid ending the phrase with a digit -- a trailing digit is read as a repeat count, so "press control 2" presses Ctrl twice instead of Ctrl+2.

**Symbols by spoken name**: these symbol names are reliably pressable -- backtick, semicolon, slash (or forward slash), backslash (or back slash), comma, period (or dot), single quote (or apostrophe), left/right bracket (also accepts open/close bracket), equals (or equal), minus (also hyphen or dash), right parenthesis (or close paren). Other symbol names are not reliable in "press" and are best avoided there: the shifted symbols (colon, tilde, pipe, question mark, double quote, braces, less than, greater than, plus, underscore, left parenthesis) come out as the wrong character, and hash, at, ampersand, asterisk, caret, percent, dollar, and exclamation press nothing at all. To type any of these characters, dictate them as punctuation words instead (see Punctuation and Symbols below) -- that path handles every symbol correctly.

**Examples**: "press control shift t", "press f5", "press alt f4", "press windows d", "press left bracket".

#### Text Formatting

All of these apply to whatever text is currently selected. Select first (with the mouse or with "select word" / "select line"), then say the command.

**Case and shape transforms**

| Say this | What happens |
|---|---|
| uppercase | Converts the selection to UPPERCASE |
| lowercase | Converts the selection to lowercase |
| capitalize | Capitalizes the first letter and lowercases the rest |
| title case | Converts the selection to Title Case |
| snake case | Converts the selection to snake_case |
| camel case | Converts the selection to camelCase |
| pascal case | Converts the selection to PascalCase |
| kebab case | Converts the selection to kebab-case |
| compress | Removes the spaces, joining the words together ("hello world" becomes "helloworld") |

**Rich text styling**

| Say this | What happens | Notes |
|---|---|---|
| x-ray bold text | Bolds the selection (Ctrl+B) | Works in apps that support rich text |
| x-ray italics | Italicizes the selection (Ctrl+I) | |
| x-ray underline | Underlines the selection (Ctrl+U) | |

**Wrapping**

These wrap your selection in the chosen characters. Said with no selection, they insert an empty pair and drop your cursor between the two characters -- handy while dictating code. You can also say them followed by the text you want wrapped.

| Say this | What happens |
|---|---|
| parentheses | Wraps the selection in ( ) or inserts an empty ( ) pair |
| parentheses hello | Inserts "(hello)" |
| brackets | Wraps in [ ] |
| braces | Wraps in { } |
| angle brackets | Wraps in < > |
| quotes | Wraps in double quotes |
| single quotes | Wraps in single quotes |

Note: when you say a wrapping word followed by more words in the same breath, those words are wrapped verbatim -- symbol words like "colon" spoken inside the wrapped text are typed literally, not converted.

#### Navigation

The "go" and "grab" commands move the cursor without touching the keyboard. "go" moves; "grab" moves while selecting along the way. You can chain several moves in one utterance with "then". The utterance must start with "go" -- "grab" works only as a step chained after a "go" move (for example "go home then grab to end"). Said on its own, "grab ..." is typed as dictation.

| Say this | What happens |
|---|---|
| go home | Jumps to the start of the line |
| go end | Jumps to the end of the line |
| go top | Jumps to the top of the document |
| go bottom | Jumps to the bottom of the document |
| go left / go right | Moves one character |
| go left 5 / go right 5 | Moves five characters |
| go right 3 words | Moves three words to the right |
| go left 2 paragraphs | Moves two paragraphs up |
| go start of word | Jumps to the start of the current word ("beginning of word" also works) |
| go end of word | Jumps forward past the current word (in most apps the cursor lands at the start of the next word) |
| go start of paragraph | Jumps to the start of the current paragraph |
| go end of paragraph | Jumps forward to the next paragraph (in most apps the cursor lands just past the end of the current one) |
| go home then grab to end | Jumps to the line start, then selects to the line end |
| go end then grab to home | Jumps to the line end, then selects back to the line start |
| go home then grab right 3 words | Selects the first three words of the line |
| go end then grab left 5 | Selects the last five characters of the line |
| go top then grab to bottom | Selects the entire document |

Counts can be digits ("3") or spoken words ("one" through "ten"; digits work up to 50). "to", "too", and "for" are accepted as sound-alikes for 2 and 4, so a recognizer that hears "go right to words" still moves two words. If any part of a "go" utterance cannot be understood, the whole phrase is typed as dictation instead -- garbled speech never produces surprise cursor movement.

#### Punctuation and Symbols

These are replacements: they work **inline during dictation**. Just say the word as part of your sentence and WheelHouse types the symbol in its place. You do not need to pause or say them as a separate command.

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

If the speech recognizer routinely mishears any other word -- for example a name heard as a sound-alike -- you can teach WheelHouse a personal correction in the Pattern Manager (say "x-ray patterns"). Your correction then applies inline during dictation, exactly like the built-in punctuation words.

#### Application Switching

| Say this | What happens | Notes |
|---|---|---|
| x-ray activate [app name] | Brings the named application forward, starting it if needed | e.g. "x-ray activate outlook", "x-ray activate code" |
| x-ray browser | Brings your default web browser to the front | WheelHouse looks up which browser is your Windows default at the moment you speak |
| x-ray notepad | Brings Notepad to the front | |

#### System

Window management and Windows itself.

| Say this | What happens | Notes |
|---|---|---|
| zoom in | Zooms in (Ctrl and plus) | |
| zoom out | Zooms out (Ctrl and minus) | |
| create tab | Sends Ctrl+N | New tab in most editors; note that in most browsers Ctrl+N opens a new window, not a tab |
| create window | Sends Ctrl+Shift+N | New window in editors; opens a private/incognito window in most browsers |
| x-ray close window | Closes the active window (Alt+F4) | Requires the hotword for safety |
| x-ray maximize | Maximizes the active window | |
| x-ray minimize | Minimizes the active window | |
| x-ray desktop | Shows the desktop (Windows+D) | |
| Windows settings | Opens the Windows Settings app | Also fires if heard as "Window settings" |

#### Mouse Control

An honest note, because many voice packages advertise this: **this release has no voice commands that move the mouse pointer** (no "mouse up", no grid overlay for pointer positioning). What WheelHouse offers instead usually covers the reasons you would want them:

- **Clicking things by voice** is handled by Voice Element Clicking (the next section) -- you click a control by saying its name or its number, which is faster and more precise than steering a pointer by voice.
- **Volume and screen brightness** are adjusted with the physical mouse thumb wheel in dedicated zones at the edges of the screen (pointer at the left edge for brightness, elsewhere for volume), not by voice. This is a deliberate design choice for people who keep one hand on a mouse or trackball.

If you need full pointer-by-voice control, WheelHouse is not that tool yet -- pair it with your preferred pointer solution and let WheelHouse handle dictation, commands, and clicking by name.

#### Voice Element Clicking

WheelHouse can click buttons, links, menu items, and other on-screen controls for you. There are two ways to pick a control: by its **name**, or by putting a **number** on every clickable control and saying the number. The numbered overlay is the answer for controls that have no obvious name to say (icon-only toolbar buttons, for example) or when several controls share the same name.

**Clicking by name**

Say "click", then the name of the control -- optionally with "the" in front (ignored) and a role word at the end (narrows the search to that kind of control). The "x-ray" hotword prefix is optional on all of the clicking commands: "click cancel" and "x-ray click cancel" both work.

| Say this | What happens |
|---|---|
| click cancel | Clicks the control named "cancel" |
| click the submit button | Clicks the button named "submit" |
| click the home link | Clicks the link named "home" |
| click the file menu | Clicks the menu named "file" |
| click remember me checkbox | Clicks the check box named "remember me" |

**Role words** you can add after the name:

- **button** -- a button
- **link** -- a hyperlink
- **menu** -- a menu item
- **tab** -- a tab
- **checkbox** (or **check box**) -- a check box
- **box**, **field**, or **input** -- a text entry field

If you say no role word, WheelHouse matches any clickable control by name. A role word said on its own with no name (for example "click button") is treated as the name, not a role -- it looks for a control literally named "button".

**The numbered overlay**

| Say this | What happens | Notes |
|---|---|---|
| apply numbers | Paints a number on every clickable control in the front window | Numbers stay up until you dismiss them |
| click 3 | Clicks the control labelled 3 | Say any visible number |
| dismiss numbers | Removes the numbers | |

Things worth knowing about the overlay:

- **The numbers stay on screen** until you say "dismiss numbers". Clicking a numbered control does not remove them -- they refresh in place so you can pick another. The numbers also follow you to whichever window is in front.
- **If a name is ambiguous, the numbers appear by themselves.** When you say "click [name]" and more than one control matches well, WheelHouse shows numbers on just the matching finalists so you can say "click [number]" to pick the one you meant.
- **Controls whose real name is a number.** While numbers are showing, saying a number always picks the numbered label -- so a control whose actual name is a digit (a calculator "7", for instance) cannot be reached by name until you say "dismiss numbers" first.
- **If the numbers look out of place, say "apply numbers" again.** After a page scrolls or swaps content, the painted numbers may sit in their old spots until you repaint them.

**What you see for each outcome**

A successful click shows no notice -- the control is simply clicked. The other outcomes show a brief advisory notice near the tray so you know why nothing happened:

- **Not found**: "No match for [name]" -- nothing on screen matched the name you said. Try the numbered overlay.
- **Ambiguous**: "Found [A] and [B] -- be more specific" -- and the numbered overlay opens on the finalists so you can pick by number.
- **Could not complete the click**: the wording names the reason, for example that the control is disabled, that the click timed out, or that the numbered overlay went stale and needs to be reapplied.

These notices are rate-limited, so a burst of failed attempts will not bury your screen in messages.

#### WheelHouse Control

Commands that steer WheelHouse itself: listening modes, help, personal patterns, and the AI features.

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening: WheelHouse listens only while you hold the floating button or tray icon | A notification confirms the switch |
| click to talk mode | Switches back to toggle listening (click to start, click to stop) -- the default | |
| x-ray wheelhouse help online | Opens the hosted WheelHouse help page in your browser | Requires the online help URL to be configured (the gem_url setting under [ai.help]); if it is not set, the command does nothing |
| x-ray patterns | Opens the Pattern Manager | "x-ray pattern manager" also works; see "Special Commands" below |
| x-ray fix | Sends the selected text to the configured AI server for grammar and polish, then replaces the selection with the corrected version | Requires the AI server to be configured and reachable; WheelHouse speaks its progress ("Correcting", "Done") and always preserves your original text on any failure |
| x-ray cancel fix | Cancels an in-progress fix | |
| x-ray boost | Adds the selected text to the speech recognition hints | See "Special Commands" below -- on the default engine this saves the hint but does not apply it until you opt in |

Turning the microphone on and off is not itself a voice command -- you click the floating microphone button or the tray icon (or, in push-to-talk mode, hold it). This is deliberate: a system that could be silenced by voice could also be silenced by a stray phrase.

About help: "wheelhouse help online" is the supported way to ask questions -- it opens the hosted help page in your browser. WheelHouse also contains an in-app help chat window, but the in-app help chat is currently disabled in this release because its answers did not meet quality standards; the voice patterns that opened it are switched off. Text correction ("x-ray fix") is the live AI feature of this release.

### Special Commands with Extra Explanation

**"literal [words]"**

Say "literal" followed by whatever you want to type, and WheelHouse inserts those exact words without running them through any command or replacement patterns. This is the escape hatch when you need to dictate a phrase that would otherwise trigger a command.

- Saying "copy" normally triggers the copy command, but "literal copy" just types the word "copy".
- "literal period" types the word "period" instead of a full stop.
- "literal new line" types the phrase "new line" instead of inserting a line break.

"literal" takes effect wherever it appears in an utterance, not only as the first word: everything you say after "literal" is typed exactly as spoken, and the word "literal" itself is not typed. A sentence with "literal" in the middle therefore types the rest of that sentence verbatim, so use it only when you actually want the escape hatch. To type the word "literal" itself, say "literal literal".

**"x-ray boost"**

When the speech recognizer keeps mishearing a specific word -- usually a name, a product, a place, or a technical term -- you can teach it to listen for that word by boosting it:

1. Select the problem word anywhere on screen (highlight it with the mouse or say "select word").
2. Say **"x-ray boost"**.

WheelHouse copies the selected text and sends it to your speech engine as a new recognition hint. The hint is saved to a shared hints file on disk, so it **persists across restarts** -- you only need to boost each tricky word once. Hints are limited to 100 characters each, so boost individual words or short phrases, not whole sentences.

One important honesty note: **saving a hint and applying it are two different things**, and what happens depends on which speech engine you use.

- **Parakeet (the default engine): the hint is saved but NOT applied out of the box.** Parakeet ships with hint biasing turned off, because applying hints slows its recognition by roughly 25 percent per utterance in the project's measurements. "x-ray boost" still records the hint. To make Parakeet actually use your saved hints, set enabled = true under the [hotwords] section of the Parakeet engine's own config file and restart WheelHouse -- accepting the slower recognition. Until you opt in, do not expect boosting to change what Parakeet hears.
- **Distil-Whisper**: applies saved hints out of the box, as biasing terms in its decoder.
- **Google Cloud Speech-to-Text**: applies saved hints out of the box, as speech adaptation phrases.

**"x-ray patterns" (the Pattern Manager)**

This opens the **Pattern Manager** window, a browsable interface that lists every voice command and text replacement WheelHouse knows. The list groups patterns by category; selecting any entry shows its details -- the trigger phrase, what it does, and whether it needs the hotword.

From the Pattern Manager you can:

- **View** any pattern, including all the built-in ones that ship with WheelHouse.
- **Create** new personal patterns -- for example a shortcut that types your email address, corrects a word the engine keeps mishearing, or opens a specific program.
- **Edit** and **delete** patterns you created yourself.
- **Customize** a built-in pattern: this creates a personal copy with the same trigger that overrides the built-in. The built-in itself is never modified, so deleting your copy restores stock behavior.
- **Change the command hotword** (the "x-ray" prefix) if another word works better for your voice.

Your personal patterns are stored in a separate per-machine file, so they survive WheelHouse upgrades, and the shipped patterns file is never touched.

**"x-ray wheelhouse help online"**

Opens the hosted WheelHouse help page in your default browser, where you can ask questions in plain language. It requires the online help URL (the gem_url setting in the [ai.help] section of the configuration) to be set; with no URL configured, the command quietly does nothing. This is the supported help path -- the in-app help chat window is currently disabled.

## Speech Modes

A common worry with voice control software is that you will have to constantly announce "command mode" or "dictation mode" and that everything falls apart when you forget. WheelHouse does not work that way. You never switch modes by hand. WheelHouse decides on the fly, word by word, whether you are giving it a command or dictating text -- and the rule it uses is simple enough that it quickly becomes second nature.

### The three things that can happen to your words

- **Command**: WheelHouse recognizes what you said as a voice command and performs it. You say "undo" and it presses the undo shortcut. You say "delete five" and it deletes five characters. Nothing gets typed.
- **Dictation**: WheelHouse types what you said into whatever text field you are working in. You say "dear Sarah thank you for the update" and those words appear in your email.
- **Inline replacement**: certain words get swapped for symbols or corrected spellings even in the middle of dictation. You say "hello comma world" and you get "hello, world" -- the word "comma" becomes the punctuation mark instead of being typed out.

### How WheelHouse decides: position determines intent

The position of a word in your phrase is what tells WheelHouse what you meant:

- **The first word of a phrase is a potential command.** When you start speaking after a pause, WheelHouse checks whether your first word could begin a known command. If it could, WheelHouse holds it very briefly (well under a second) to see whether the next word or two completes the command. Say "delete five" as its own phrase and the command runs. If the words turn out not to match any command after all, they are typed as ordinary text -- nothing is ever lost.
- **Words in the middle of a phrase are dictation.** Say "I want to delete five items" and the whole sentence is typed, including the word "delete". Because "delete" arrived mid-sentence, WheelHouse knows you meant it as text, not as an instruction. This is why you can dictate naturally without tiptoeing around command words.
- **Replacement words work anywhere.** Words like "comma" and "period" are substituted whether they come first, last, or mid-sentence, because their whole job is to appear inside dictation.

### The hotword safety gate

Some commands could do real damage if they fired by accident while you were dictating -- closing a window, for example. Those commands are protected by a safety word: they only run when you say "x-ray" first, as in "x-ray close window". Everyday low-risk commands do not need it. And the hotword follows the same position rule as everything else: "x-ray" only has its special meaning as the very first word of a phrase. Mention x-ray machines in the middle of a sentence and the word is simply typed. If you say "x-ray" and what follows is not actually a command, the whole phrase (including "x-ray") is typed as text -- again, no words are ever lost.

### Words appear as you speak

WheelHouse streams your speech. You do not talk, stop, and wait for a block of text to appear -- words show up on screen while you are still talking, flowing out one after another. The only exception is that tiny hold at the start of a phrase while WheelHouse checks whether you are giving a command, and a similar brief hold around replacement words; both are fractions of a second.

### Chaining cursor moves with "then"

You can chain cursor movements and text selections into one phrase by saying "then" between them:

- "go home then grab to end" -- jumps to the start of the line, then selects everything to the end of the line.
- "go top then grab to bottom" -- jumps to the top of the document, then selects everything to the bottom.

This chaining works only for the "go" (move the cursor) and "grab" (select text) navigation commands. Other commands -- copy, paste, switching windows, and so on -- are each spoken as their own separate phrase.

## Interaction Modes: Toggle vs Push-to-Talk

Speech modes (above) are about what WheelHouse does with your words. Interaction modes control something more basic: when WheelHouse listens at all. There are two, and you can switch between them at any time.

### Toggle mode (the default)

WheelHouse listens continuously whenever speech is switched on. One click on the floating on-screen button -- or one left-click on the WheelHouse icon in the system tray -- turns listening off; another click turns it back on. This is the mode for hands-free use: once listening is on, you never need to touch anything again.

A bonus even in toggle mode: press and hold the floating button (about a fifth of a second or longer) and WheelHouse listens only for as long as you hold it, like a walkie-talkie, then goes back to normal when you release. Handy when you mostly keep listening off but want to speak one quick command.

### Push-to-talk mode

WheelHouse listens only while you are physically holding down the floating button. Press and hold to talk; release and listening stops instantly. While you hold, WheelHouse also mutes your computer's speakers so that sound from a video or music cannot leak into the microphone and be transcribed -- your volume is restored the moment you release. In this mode, a single left-click on the tray icon does nothing; the hold works on the floating button.

Two things worth knowing:

- **Safety release.** If a hold somehow gets stuck (say the release never registered), WheelHouse automatically stops listening after 30 seconds and restores your audio, so you are never left with a live microphone or muted speakers. If you dictate long passages in this mode and the 30-second cutoff interrupts you, you can raise it with the ptt_safety_timeout_seconds setting in the [speech] section of the config file.
- Push-to-talk needs a hand on the mouse (or a finger on a touchscreen), so it trades away some of the hands-free benefit that is WheelHouse's main point.

### How to switch between the modes

Any of these works, at any time:

- **By voice**: say "push to talk mode" to switch to push-to-talk, or "click to talk mode" to switch back to toggle mode.
- **Tray menu**: right-click the WheelHouse icon in the system tray and click "Push-to-Talk Mode". A checkmark on that menu item shows when push-to-talk is active.
- **Double-click**: double-click the floating button or the tray icon to flip between the two modes.
- **At startup**: the interaction_mode setting in the [speech] section of the config file ("toggle" or "push_to_talk") sets which mode WheelHouse starts in. The voice, menu, and double-click switches change it while WheelHouse is running.

### Which should you use?

Stay with toggle mode if you want hands-free control -- it is the default for a reason, and it is the mode most people should use. Choose push-to-talk when you are in a noisy room, when other people's voices or your speakers keep getting transcribed, or when you use voice input only occasionally and want to be certain WheelHouse hears nothing between holds.

## Configuration

You do not need to edit any settings to use WheelHouse. Every value ships with a working default, and the most common choices (which speech engine to use, push-to-talk versus click-to-talk) can be changed from the tray menu or by voice without ever opening a file. This section exists for the day you want to fine-tune something.

WheelHouse keeps its settings in a plain text file called config.toml. It is an ordinary text file you can open in Notepad. The installer creates it for you from a template, and your copy is personal to your machine -- it is never sent anywhere. Lines that start with a number sign are comments; the file explains many of its own settings inline.

A few practical notes before the reference:

- Change one thing at a time, then restart WheelHouse so the change takes effect.
- If you make a mistake and something stops working, you can restore the defaults by copying the shipped template (config.toml.example, in the same folder) over your config.toml.
- Settings marked "device-specific" are off by default and only matter if you own that piece of hardware. WheelHouse runs fine with all of them turned off.

### General Settings (top of the file)

**SPEECH_WEBSOCKET_HOST** -- The network address the speech engine uses to talk to the rest of WheelHouse. Default: 127.0.0.1, which means "this computer only" -- nothing leaves your machine. Change it only in the advanced setup where speech recognition runs on a second computer on your home network; otherwise leave it alone.

**REPLACEMENT_TIMEOUT_MS** and **COMMAND_TIMEOUT_MS** -- How long, in milliseconds, WheelHouse waits after you stop speaking before it decides a command or a correction phrase is complete. Default: 700 for both. If commands seem to fire before you finish your sentence -- common on slower machines -- raise these (try 900 or 1000). If WheelHouse feels sluggish to respond, you can lower them slightly.

**GREEDY_TIMEOUT_MS** -- A longer wait used for commands that intentionally keep listening for more words. Default: 5000 (five seconds). Rarely needs changing.

**COMMAND_COMPLETION_WAIT_MS** -- A short pause after a command finishes, so a fast follow-up command does not collide with it. Default: 1000. Raise it on a slow machine if back-to-back commands step on each other.

**ENABLE_AUDIO_SUPPRESSION**, **ENABLE_SONOS_SUPPRESSION**, **ENABLE_IDLE_SUPPRESSION** -- Three on/off switches (true or false) that pause listening when other audio is playing on the computer, when Sonos speakers are playing, or when the computer has been idle for a while. Default: all true. Turn one off if you actually want WheelHouse listening during music or video playback -- but expect more misrecognitions, because the microphone will pick up the audio.

**LOG_FILE** and **LOG_LEVEL** -- Where WheelHouse writes its activity log and how detailed that log is. Defaults: an empty LOG_FILE (the standard log location) and DEBUG detail. You would only change these when a support conversation asks you to.

**LOG_TRANSCRIPTS** -- A privacy setting. Default: false, which means the log files never contain the words you dictate or anything from your clipboard -- only a note of how long the text was. Set it to true only while troubleshooting recognition problems, and turn it back off afterward: while it is on, everything you dictate, including passwords, accumulates in the log files on your disk.

**SIDE_OFFSET** -- WheelHouse can use a mouse thumb wheel as a volume and brightness control. When the mouse pointer is within this many pixels of the left edge of the screen, the wheel adjusts brightness; anywhere else it adjusts volume. Default: 10. Raise it if you find the brightness zone too hard to hit.

**BRIGHTNESS_INCREMENT** and **VOLUME_INCREMENT** -- How big each step of that thumb-wheel adjustment is. Defaults: 1.0 for brightness, 0.5 for volume. Raise them for faster, coarser changes; lower them for finer control.

**FLOATING_BUTTON_SIZE**, **FLOATING_BUTTON_POS**, **FLOATING_BUTTON_VISIBLE** -- The small on-screen status button. Size in pixels (default 30), position as an offset from a screen corner (default -18, -15), and whether it shows at all (default false -- hidden). Set FLOATING_BUTTON_VISIBLE to true if you want an always-visible click target for the microphone, which is especially handy in push-to-talk mode.

**SPEECH_ENABLED_ON_STARTUP** -- Whether WheelHouse starts listening as soon as it launches. Default: true. Set false if you prefer to turn the microphone on manually each session.

**SHOW_SPEECH_PULSE** -- Whether the tray icon pulses while WheelHouse hears you speaking. Default: true. It is a useful "yes, I can hear you" signal; turn it off only if you find the animation distracting.

**SPATIAL_SOUND_EXEC** and **SPATIAL_SOUND_FORMAT** -- Support for switching Dolby Atmos spatial sound by voice, using a small free helper tool from NirSoft. Default: empty, which leaves the feature off. Only fill these in if you use Dolby Atmos and have that tool installed; everyone else can ignore them.

### Brightness Coordinator ([brightness_coordinator])

WheelHouse changes screen brightness in layers: real hardware brightness first (a supported TV or the laptop panel), then a software dimming effect once the hardware is as low as it goes. These settings tune that handoff. Most people never touch this section.

**software_dimmer** -- Which software dimming method to use when hardware brightness runs out. Default: gamma_dimmer, which darkens the screen through the graphics card. The alternative is a translucent overlay window, or the default "flux" style that works through a companion dimming app's hotkeys. Change it only if dimming misbehaves with your particular monitor setup.

**unwinding_threshold** -- The brightness level (default 10) at which WheelHouse starts undoing software dimming and handing control back to the hardware as you brighten the screen.

**flux_transition_percent** -- How many percent of brightness each simulated hotkey press represents when driving a companion dimming app. Default: 2.

**flux_dim_hotkey** and **flux_brighten_hotkey** -- The keyboard shortcuts WheelHouse presses to drive that companion app. Defaults: Alt+PageDown to dim, Alt+PageUp to brighten. Change these only if you have remapped the app's own hotkeys.

### Plugins ([plugins.*])

Every plugin has its own [plugins.*] section with an enabled switch. All of
them -- what each plugin does, every setting with its default, and
troubleshooting basics -- are covered in the Plugins section later in this
document.

### Wake Word ([wake_word])

After an idle pause, you can wake WheelHouse by saying its wake word out loud -- no keyboard or mouse needed. This runs entirely on your computer.

- **enabled** -- On/off. Default: true.
- **keyword** -- The wake word. Default: "computer".
- **sensitivity** -- How easily the wake word triggers, from 0 to 1. Default: 0.5. Raise it if saying "computer" often fails to wake WheelHouse; lower it if ordinary conversation keeps waking it by accident.
- **mode** -- What the wake word is used for. Default: "idle_recovery", meaning it wakes WheelHouse from an idle pause.
- **model_dir** -- Where the wake-word listening model lives on disk. Set by the installer; do not change it.

### Text Insertion Fine-Tuning ([ui_actions.*])

These settings govern the mechanics of how dictated text lands in other programs. The defaults are tuned carefully; change them only when troubleshooting a specific symptom.

#### Timing ([ui_actions.timing])

All values are in milliseconds unless noted. On older or heavily loaded machines, raising these can fix text that arrives garbled, half-pasted, or out of order:

- **clipboard_verification_timeout_ms** (default 250) -- how long WheelHouse waits to confirm the clipboard operation worked.
- **clipboard_operation_delay_ms** (default 50) -- a small pause around clipboard use.
- **selection_clear_delay_ms** (default 20), **context_gather_delay_ms** (default 10), **post_paste_delay_ms** (default 30) -- brief pauses that keep fast programs and slow programs in step.
- **utterance_clipboard_timeout_seconds** (default 60.0) -- how long, in seconds, a copied utterance stays available for the "paste that" style of command.

#### Short-text typing ([ui_actions.verified_unicode])

**max_chars** (default 50) -- Dictations up to this length are typed directly, character by character, which avoids touching your clipboard at all. Longer dictations go through the clipboard because typing them out would be slow. Lower this if a particular app mishandles direct typing; raise it if you want more dictations to bypass the clipboard.

#### Browser recognition ([ui_actions.foreground_check])

**same_process_browser_names** -- The list of web browsers WheelHouse recognizes, which it needs because browsers manage their windows in an unusual way. All the mainstream browsers are already listed. **same_process_browser_names_extend** lets you add an unusual browser to the list without retyping the built-in ones.

#### Dictation safety lists ([ui_actions.text_target])

Before typing anywhere, WheelHouse checks that the spot your cursor is in really accepts text -- this is what prevents dictation from spraying keystrokes into the wrong place. The four settings here (**allow_class_names_extend**, **deny_control_types_extend**, **deny_class_names_extend**, **browser_process_names_extend**) let you extend the built-in allow and deny lists for an unusual app. Default: all empty. Most people should use the built-in approval prompt instead of editing these -- when WheelHouse is unsure about a text box, it asks you on screen, and remembers your answer.

### Speech Interaction ([speech])

- **interaction_mode** -- "toggle" (default) means the microphone stays on until you turn it off: click once to start, click again to stop. "push_to_talk" means WheelHouse listens only while you hold down the tray icon or floating button, and mutes system audio while you hold. You can also switch by voice ("push to talk mode", "click to talk mode") without editing anything.
- **ptt_safety_timeout_seconds** -- In push-to-talk mode, a safety net that automatically releases the microphone if a hold gets stuck (for example, if the button was held when a window stole focus). Default: 30 seconds. Raise it if you routinely dictate longer than 30 seconds in one hold.
- **notify_on_revision** -- Whether to show a small notice when the speech engine revises its guess at what you said. Default: false.

### Speech Recognition Engine ([stt])

**last_provider** -- Which speech-to-text engine WheelHouse uses. Default: "parakeet_tdt", the local engine that runs entirely on your computer with no account and no internet after setup. The other options are "distil_medium_en" (a more accurate local engine that needs a recent graphics card) and "google_stt" (Google's cloud service; needs a Google Cloud account, sends audio to Google). You normally switch engines from the tray menu rather than editing this -- WheelHouse writes your choice here for you, which is why it is called "last" provider.

**[stt.google] boost_words** -- A list of words or phrases the Google engine should favor when unsure -- useful for names or uncommon words it keeps getting wrong. Default: empty. Only matters if you use the Google engine.

**[stt.azure]** -- Credentials for the Azure cloud speech option: **subscription_key** (default empty) and **region** (default "eastus"). Only matters if you deliberately set up Azure; most people never touch this.

### AI Features ([ai], [ai.server], [ai.help])

WheelHouse's AI features are optional and off unless you point them at an AI server. In this release, the live AI feature is dictation text correction -- fixing up dictated text on request. The in-app help chat is currently disabled; these settings also gate it, but it will not appear regardless of what you set.

**[ai] enabled** -- The master switch for all AI features. Default: true, but nothing happens unless a server address is also configured below. Today this means dictation text correction; it also gates the in-app help chat, which is currently disabled in this release.

**[ai] knowledge_base** -- The help document the in-app help assistant would consult when answering questions. Because the in-app help chat is currently disabled, this setting has no effect today; it is kept for a future release. Default: the shipped help document.

**[ai.server] base_url** -- The address of the AI server WheelHouse talks to, using the standard OpenAI-style interface. Default: a local Ollama server on your own machine (http://localhost:11434/v1). Any OpenAI-compatible address works here, local or hosted. Leave it empty to turn AI off entirely.

**[ai.server] model** -- The name of the AI model to request from that server. Default: "gemma3:12b". Change it to whatever model your server has installed.

**[ai.server] kind** -- "local" or "remote". Default: "local". This is how you tell WheelHouse whether the server is on your own machine or out on the internet -- and it frames the privacy tradeoff: with a local server, the text being corrected never leaves your computer; with a remote one, it is sent to that service.

**API credential** -- There is deliberately no key stored in the config file. If your server needs a credential (a cloud service usually does; a local Ollama does not), set it in the WHEELHOUSE_AI_API_KEY environment variable in Windows instead. That way the secret never sits in a settings file that could be copied or shared. To set it: Windows Settings, search for "environment variables", choose "Edit environment variables for your account", add a new variable named WHEELHOUSE_AI_API_KEY with your key as the value, then restart WheelHouse.

**[ai.server] timeout_s** -- How many seconds WheelHouse waits for the AI server to answer before giving up on that request. Default: 30. Raise it if a slow local model keeps timing out.

**[ai.help] gem_url** -- The web address that the voice command "wheelhouse help online" opens in your browser. This hosted help page is the help surface of the current release, since the in-app help chat is currently disabled. Default: empty (the command does nothing until a page is configured).

**[ai.help] max_response_tokens** -- Caps the length of an answer from the in-app help chat; because that chat is currently disabled, this setting has no effect today. Default: 800.

**If the AI server is unreachable**, nothing breaks: the AI features quietly turn themselves off, and dictation, voice commands, and everything else keep working exactly as before. AI is a convenience layered on top of WheelHouse, never a requirement.

### Terminal Dictation ([terminal])

**submit_delay_ms** -- When you dictate into a terminal window through WheelHouse's terminal editor, this is the brief pause (default 100 milliseconds) between delivering the text and pressing Enter. Raise it if a slow terminal occasionally drops the end of a line.

### Voice Clicking ([click])

Settings for the "click ..." commands that let you press buttons and links by naming them, and for the numbered overlay ("apply numbers", then "click 5"). The defaults work well; the ones a user might plausibly adjust:

- **enabled** -- Master switch for voice clicking. Default: true.
- **min_confidence** (default 0.4) and **clear_winner_margin** (default 0.15) -- How sure WheelHouse must be before clicking something by name, and how clearly one candidate must beat the runner-up. Raise min_confidence if it clicks the wrong thing; lower it if it too often says it cannot find a match. When there is no clear winner, WheelHouse shows the numbered overlay instead of guessing.
- **notice_max_names** (default 3) -- How many candidate names appear in the "did you mean" style notice.
- **overlay_badge_font_pt** (default 12) -- The size of the numbers painted on screen in overlay mode. Raise it if the numbers are hard to read.
- **response_timeout_ms** (default 3000) and **walk_deadline_ms** (default 2500) -- How long WheelHouse spends searching a window for clickable things before giving up. Raise both on a slow machine if voice clicks report a timeout in complex windows.
- **snapshot_ttl_seconds** (default 30) -- How long the numbered overlay's snapshot of the screen stays valid.
- **browser_processes** and **browser_processes_extend** -- The list of browser-like apps (browsers, Slack, Discord, and similar) that need a deeper search to find their buttons. Add an app to the extend list if voice clicking cannot see controls inside it.
- **enable_screen_reader_flag** (default false) -- Tells apps a screen reader is present, which makes some of them expose more clickable elements. Try true if a particular app hides its buttons from voice clicking; note some apps change their appearance when this is on.

The remaining click settings (tiebreaker distances, substring matching thresholds, fallback switches) are fine-tuning knobs best left at their defaults.

### Slow Machine Tweaks

If WheelHouse feels laggy or unreliable on an older computer, these specific changes help, roughly in order of impact:

1. **Use the default speech engine.** "parakeet_tdt" (the shipped default under [stt] last_provider) is the lightest local engine and runs on any CPU. Do not switch to "distil_medium_en" on a machine without a capable recent graphics card. If even the default struggles, "google_stt" moves the heavy work to the cloud entirely -- at the cost of needing an account and an internet connection.
2. **Give yourself more speaking time.** Raise REPLACEMENT_TIMEOUT_MS and COMMAND_TIMEOUT_MS from 700 to 900-1000 so commands stop firing before you finish, and raise COMMAND_COMPLETION_WAIT_MS from 1000 to 1500 if quick back-to-back commands collide.
3. **Slow down text insertion.** Under [ui_actions.timing], raise post_paste_delay_ms (30 to 60), clipboard_operation_delay_ms (50 to 100), and clipboard_verification_timeout_ms (250 to 500) if dictated text arrives incomplete or garbled in slow programs.
4. **Give voice clicking more time.** Under [click], raise response_timeout_ms (3000 to 5000) and walk_deadline_ms (2500 to 4000) if clicks time out in complex windows.
5. **Be patient with a local AI server.** If you run one and corrections time out, raise [ai.server] timeout_s from 30 to 60 -- or simply leave AI off; nothing else depends on it.

### Speech Recognition Quality Tweaks

**The hallucination filter (Distil-Whisper engine only).** Speech engines in the Whisper family have a well-known quirk: fed a cough, a throat-clear, or background noise, they sometimes invent polite filler -- a stray "thank you" or "okay" appears that you never said. The Distil-Whisper engine ships with a confidence filter that suppresses these: when the engine's own confidence in an utterance is too low, WheelHouse discards it instead of typing it.

The filter's threshold is **hallucination_logprob_threshold** in the Distil-Whisper provider's own config file (not the main config.toml), and its default is -0.55. That default was calibrated on a single male voice using a studio microphone -- so it may be too strict for other voices and setups. If you use the Distil-Whisper engine and notice that real speech is sometimes silently ignored -- more likely with a strong accent, quiet speech, or a laptop microphone -- lower the threshold to -0.7 or -0.8. More negative means more permissive: fewer real words discarded, at the cost of letting the occasional phantom "thank you" through. Setting it to a very large negative number turns the filter off entirely.

If no threshold feels right for your voice, there is an escape hatch: switch to the Google engine from the tray menu, which handles noise and varied voices more robustly (it is a cloud service, so it needs an account and sends audio to Google).

This filter does not apply to the default Parakeet engine -- its design does not produce the confidence signal the filter relies on, and it does not share the Whisper family's phantom-phrase quirk to the same degree.

**Boosting words the engine keeps missing.** If you use the Google engine and it consistently mishears a particular name or technical term, add that word to boost_words under [stt.google] to tip recognition in its favor.

## Plugins

Plugins are optional add-ons that connect WheelHouse to extra hardware and services: your laptop screen, Sonos speakers, a Sony TV, and a few Windows features. Every plugin has its own `[plugins.*]` section in config.toml with an `enabled` switch, so you can turn each one on or off without deleting anything. You do not need any of them for dictation and voice commands to work, and a plugin whose hardware is missing or offline never breaks WheelHouse -- it simply sits quietly and keeps retrying in the background.

Two of these plugins respond to the mouse thumb wheel using screen "scroll zones": with the pointer on the left side of the screen, the wheel adjusts brightness; anywhere else, it adjusts volume.

### Internal Panel

Controls the brightness of a laptop's built-in screen from the brightness scroll zone. Enable or disable with `plugins.internal_panel.enabled` (default: enabled). There are no other settings -- everything is detected automatically. It talks to the laptop display through a built-in Windows interface, entirely on your own machine. On a desktop PC with no built-in panel it does nothing and is safe to leave enabled.

### Sonos

Adjusts Sonos speaker volume from the volume scroll zone, and pauses WheelHouse's listening while music is playing so song lyrics are not typed into your documents. Enable with `plugins.sonos.enabled` (default: disabled -- turn it on only if you own Sonos speakers). Settings:

- `polling_interval` -- how often, in seconds, to check whether music is playing (default 2).
- `speaker_ip` -- optional. WheelHouse finds Sonos speakers on your network automatically; set this only if discovery fails or you want a specific speaker.
- `request_connect_timeout` / `request_read_timeout` -- advanced network timeouts (defaults 2.0 and 5.0 seconds); rarely need changing.

It connects to the speaker over your home network directly -- no Sonos account or internet service is involved. Sound coming from your computer or TV through the Sonos does not pause listening; only streamed music does.

### System Volume

Controls the normal Windows volume (the same one as the taskbar speaker icon) from the volume scroll zone, and quiets system audio while you hold the push-to-talk button. Enable with `plugins.system_volume.enabled` (default: enabled). Settings:

- `device_type` -- which audio device to control: `"default"` (the usual choice), `"communications"`, or a specific device name.
- `volume_step_db` -- loudness change per wheel step, in decibels (default 1.5).
- `min_volume_db` / `max_volume_db` -- the volume floor and ceiling (defaults -96.0 and 0.0).

Fully local, no network. Both volume plugins can stay enabled: at startup WheelHouse picks one -- Sonos when your audio is actually playing through a Sonos, System Volume otherwise -- so they never fight.

### Bravia (Sony TV)

Brings a Sony Bravia TV used as a computer monitor into WheelHouse's brightness control, so the brightness scroll zone can dim and brighten the TV itself. Enable with `plugins.bravia.enabled` (default: disabled). Settings:

- `ip_address` -- your TV's address on the home network. Optional: leave it blank and WheelHouse searches the network for the TV automatically; set it if you have more than one TV or discovery fails.
- `psk` -- the pre-shared key you set on the TV under Settings -> Network -> Home Network -> IP Control. Required; the plugin will not start with it blank.
- `device_name` -- the TV's audio device name exactly as Windows shows it under Sound settings -> Output (default "SONY TV"). This is not a label you invent: WheelHouse uses it to look the device up for spatial-sound handling, so it must match the Windows name exactly.

It connects to the TV over your home network using Sony's built-in remote-control interface. The plugin first checks whether a Sony display is physically connected; on a machine without one it goes quietly inactive, so leaving it configured on a laptop you travel with is harmless.

### Idle Monitor

Notices when you have stepped away (no keyboard or mouse activity) and pauses listening so WheelHouse is not transcribing an empty room; listening resumes when you return or say the wake word. Enable with `plugins.idle_monitor.enabled` (default: enabled). Settings: `idle_timeout_minutes` (default 10) and `polling_interval_seconds` (default 4). Fully local -- it only asks Windows how long since your last keypress or mouse move. Almost everyone should leave this on.

### Window Positioning

Automatically moves the Windows On-Screen Keyboard out of the way when it would cover the window you are working in. Enable with `plugins.window_positioning.enabled` (default: enabled). Settings: `target_window_names` (which windows to move; default is the On-Screen Keyboard), `move_cooldown_seconds` (default 0.5, prevents jitter), `clearance_gap_pixels` (default 5), and `ignore_window_titles` / `ignore_window_classes` (windows that should never trigger a move). Fully local.

### A note on the software dimmer section

You may see a `[plugins.software_dimmer]` block in config.toml. It is a leftover -- WheelHouse does not read it. The screen-dimming method is chosen by the `software_dimmer` key in the `[brightness_coordinator]` section instead, and the shipped default there works for most people.

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

- Confirm the plugin's `enabled = true` and restart WheelHouse -- plugins are only discovered at startup.
- Check the log's startup lines: each plugin reports whether it initialized, went inactive (hardware not found), or failed, usually with the reason.
- For Sonos and Bravia, make sure the device is powered on and reachable from this PC on the same network.
- For Bravia specifically, IP Control must be enabled on the TV and the pre-shared key in config.toml must match the one set on the TV.
- If the mouse wheel does nothing, check the scroll zones: pointer on the left side of the screen adjusts brightness, anywhere else adjusts volume -- and at least one plugin for that control type must be enabled.

## Troubleshooting

Most problems have simple causes, and none of them mean your computer is broken or that you did something wrong. Work through the checklist first -- it finds the problem in most cases -- then look up the matching entry below.

### First-Time Setup Checklist

Walk through these five checks in order. Stop at the first one that fails and jump to the entry it names.

1. **Did the installer finish without red error lines?** If not, see "Installer failures."
2. **Do Windows Sound settings show your microphone picking up sound?** Right-click the speaker icon on the taskbar -> Sound settings -> Input, then speak. Does the input meter move? If not, see "Microphone not detected."
3. **Is the WheelHouse icon visible in the system tray?** If it is missing, see "WheelHouse does not start or the tray icon is missing."
4. **Open Notepad, click in the empty page, and say "hello". Does the word appear?** If not, see "Dictation not appearing in text fields."
5. **Now say "undo". Does the word disappear?** If not, see "Commands not recognized."

If all five pass, WheelHouse is working -- any remaining trouble is specific to one app or one feature, and the entries below cover the common cases.

### Common Problems

**Microphone not detected**

- *What you see:* WheelHouse starts, but nothing happens when you speak, and Windows Sound settings show no input activity.
- *What is likely wrong:* Windows is using a different microphone than the one you are speaking into, or a privacy setting is blocking desktop apps from the microphone.
- *What to try:* Open Settings -> Privacy and security -> Microphone and make sure "Let desktop apps access your microphone" is on. Then open Sound settings -> Input and pick the microphone you actually use. Restart WheelHouse afterward so it picks up the change.

**WheelHouse does not start or the tray icon is missing**

- *What you see:* You start WheelHouse and nothing appears, or the tray icon never shows up.
- *What is likely wrong:* One of WheelHouse's background processes failed during startup -- most often because a speech model is missing or an earlier install was interrupted.
- *What to try:* Re-run the one-line install command. Re-running the installer is always safe: it repairs a broken install and keeps your settings, your personal voice patterns, and the downloaded speech model. If it still will not start, restart the computer and try once more before reaching out for help.

**Speech engine not connecting**

- *What you see:* The tray icon shows the speech engine as disconnected, or WheelHouse seems to be waiting forever for speech to start working.
- *What is likely wrong:* The speech engine failed to start. Common reasons: its model was never downloaded, the Google Cloud engine has no credentials, or the computer is low on memory.
- *What to try:* Switch engines from the tray menu -- Parakeet is the built-in offline engine and needs no account. If the engine you want was never fully set up, re-run the installer and choose it at the engine question; the installer downloads whatever that engine needs. For the Google Cloud engine, check that the GOOGLE_APPLICATION_CREDENTIALS environment variable points at your credentials file (see the speech engine section of this document).

**Commands not recognized**

- *What you see:* You say "undo" and nothing happens, or the word appears as typed text instead.
- *What is likely wrong:* The speech engine misheard you (for example "undue" instead of "undo"), or you spoke while other audio was playing and the words ran together.
- *What to try:* Speak a little more deliberately, with a brief pause before the command. If one particular word is misheard over and over, select a correctly spelled copy of it anywhere on screen and say "x-ray boost" -- that teaches the speech engine to expect that word from now on.

**Command words are typed as text instead of doing anything**

- *What you see:* You say "close window" and the words "close window" appear in your document instead of the window closing.
- *What is likely wrong:* Nothing is broken. Commands that could be destructive need the safety word "x-ray" in front, so they can never fire by accident while you are dictating a normal sentence.
- *What to try:* Say "x-ray close window". If you are not sure whether a command needs the safety word, the command list in this document marks the ones that do.

**Dictation not appearing in text fields**

- *What you see:* You speak, WheelHouse clearly hears you, but no text appears in the app you are looking at. You may see a small notice saying WheelHouse was not sure the spot you are in accepts text.
- *What is likely wrong:* Either the text field is not actually focused (clicked into), or WheelHouse could not confirm that the focused spot is a real text box. This caution is deliberate: typing into the wrong place in some apps -- especially web browsers -- can trigger keyboard shortcuts instead of entering text, so WheelHouse refuses rather than guesses.
- *What to try:* Click directly inside the text field and try again. If a notice appears with a "Try it anyway" button, use it -- if the text lands correctly a few times, WheelHouse remembers that spot and stops asking. To confirm WheelHouse itself is fine, test in Notepad: if Notepad works, the problem is that one app's unusual text field, not your setup.

**Real speech silently ignored, or short dictations disappear**

- *What you see:* With the Distil-Whisper (graphics card) engine, occasional short phrases -- or for some voices, quite a lot of real speech -- produce nothing at all, with no error.
- *What is likely wrong:* That engine has a built-in filter that discards sounds it judges to be noise rather than speech -- coughs, throat clears, a door closing. The filter is tuned for a typical voice on a good microphone. If you have a strong accent, speak quietly, or use a laptop's built-in microphone, the filter can misjudge your real speech as noise and silently drop it.
- *What to try:* Make the filter less strict: in the Distil-Whisper engine's own config file, lower the hallucination_logprob_threshold setting from its default of -0.55 to -0.7 or -0.8 (more negative means less strict), then restart WheelHouse. If tuning does not help your voice, switch to a different engine from the tray menu -- the Google Cloud engine does not use this filter.

**The floating button keeps pulsing after a cough or throat clear**

- *What you see:* On older versions, a cough or other short noise could leave the listening indicator pulsing for a long time even though nothing was being heard.
- *What is likely wrong:* This was a known bug and has been fixed -- current versions reset the indicator on their own within a few seconds.
- *What to try:* If you still see it, update WheelHouse by re-running the one-line install command.

**Everything freezes after clicking inside WheelHouse's console window**

- *What you see:* On older versions, clicking and dragging inside the black console window that opens with WheelHouse could freeze the whole app until you pressed the Escape key in that window.
- *What is likely wrong:* A Windows console feature called QuickEdit pauses programs while text is selected in their console. This was a known issue and has been fixed -- current versions turn that feature off at startup.
- *What to try:* If it happens, click on the console window and press Escape to unfreeze everything, then update WheelHouse by re-running the install command.

**AI text correction does nothing or times out**

- *What you see:* Dictated text is not being cleaned up even though AI is turned on. (Text correction is the AI feature in this release; the in-app help chat is currently disabled.)
- *What is likely wrong:* WheelHouse does not run the AI itself -- it sends requests to a separate AI server you point it at. If that server is missing, unreachable, slow, or does not have the requested model, the AI features quietly switch off while everything else keeps working.
- *What to try, in order:*
  1. In your settings file, confirm the [ai] section has enabled = true and that [ai.server] base_url is filled in. An empty base_url turns AI off on purpose.
  2. Confirm the AI server is actually running and reachable at that address, and that the [ai.server] model name is one that server really offers.
  3. If the server is just slow to answer, raise [ai.server] timeout_s so WheelHouse waits longer before giving up.
  4. For a remote server that requires a key, set the WHEELHOUSE_AI_API_KEY environment variable to your key and restart WheelHouse. The key lives only in that environment variable, never in the settings file.
- *Reassurance:* An unreachable AI server never breaks WheelHouse. Dictation, voice commands, and everything else keep working with AI off.

**Installer failures**

Every failure message the installer prints -- low memory, low disk space, a blocked uv download, an integrity-check failure, an interrupted download, or WheelHouse still running during an update -- is explained one by one in the "What failure looks like" part of the Getting Started section, along with what to do about each. The short version: re-running the installer is always safe, downloads resume where they left off, and every message is safe to paste into a help request.

---

## Getting Help

If the answer is not in this document, you can reach the developer at the WheelHouse project page: https://github.com/wheelhouse-project/WheelHouse -- open an issue or start a discussion there. Include what you tried, what you expected, and what happened instead; if the installer printed an error, paste it in full. WheelHouse is actively developed, and real-world reports like yours are how it improves.

---

Generated: 2026-04-07 (regenerated 2026-07-17 from current sources for the v1.0.2 release, wh-help-doc-regen)
WheelHouse version: 1.0.2
