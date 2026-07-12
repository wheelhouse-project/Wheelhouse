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
- For general computing questions (installing Python, microphone setup, Windows
  settings): help freely using your general knowledge.
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

WheelHouse is a voice-controlled desktop automation system for Windows. You speak, and your PC responds -- issue commands, dictate text, switch between applications, adjust volume, control screen brightness, all without touching the keyboard or mouse.

**Who is it for?** Anyone who wants hands-free control of their computer: people with mobility or RSI concerns, programmers who'd rather talk than type, and anyone curious about a faster, more natural way to interact with Windows.

**What do you need?**
- A Windows 10 or Windows 11 PC
- A microphone (built-in laptop mic works; a dedicated mic gives better results)
- Python 3.12 (the setup script installs it for you if it's missing)

**How it works, in one paragraph.** You speak into your microphone. WheelHouse captures the audio, sends it to a speech recognition engine that turns your words into text, and then decides what to do with those words. If they sound like a command ("undo", "copy", "zoom in"), WheelHouse runs the command. If they sound like regular dictation ("dear Sarah, thank you for your message"), WheelHouse types the text into whatever window is currently focused -- a document, email, chat, code editor, or anything else that accepts text. Punctuation, capitalization, and formatting are handled automatically. Text starts appearing about 1.5 to 2 seconds after you start speaking and keeps flowing continuously as you talk. You don't wait for silence.

---

## Day 1 Quick Start

**Stop here if you're new. Do these steps first. Ignore everything else in this document until you've done them.**

1. Open PowerShell and run the setup script:
   ```
   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
   ```
2. Start WheelHouse. A floating button appears on your screen and a tray icon appears in your system tray.
3. Open Notepad.
4. Say **"hello world"** -- the words "hello world" should appear.
5. Say **"new line"** -- the cursor moves to a new line.
6. Say **"undo"** -- the text is undone.
7. Say **"select all"** -- the text is highlighted.

**That's it. You're using WheelHouse.** Everything below is for when you're ready to learn more.

One choice worth knowing about on day 1: WheelHouse starts in **toggle mode** (it listens continuously while enabled). If you'd rather hold a button while you speak -- walkie-talkie style -- see "Interaction Modes: Toggle vs Push-to-Talk".

---

## Pick Your Path

Now that the basics work, pick the path that matches what you want to do next.

- **"I just want to dictate text into emails, documents, and chat."** Go to: Voice Commands (especially the Text Editing and Punctuation & Symbols subsections), then Speech Modes.
- **"I'm a programmer or power user and want to see everything WheelHouse can do."** Go to: Voice Commands (full reference), Configuration Reference, Plugins.
- **"I'm trying to set things up and something isn't working."** Go to: Getting Started (full version), then Troubleshooting.
- **"I want to know if this will even run on my PC."** Go to: Can My Computer Handle WheelHouse?

---

## Getting Started (Full Version)

### Installation

Run the bootstrap script from the repository root in PowerShell:

```
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

The script checks for Python 3.12 and uv, installs them via Windows Package Manager (winget) if they're missing, runs `uv sync` in each WheelHouse service directory, installs Ollama (used by the code indexer for local embeddings), and starts the code indexer.

### What successful installation looks like

You'll see a series of status lines along the lines of `[+] Python 3.12 found`, `[+] uv installed`, `[+] uv sync: services/wheelhouse`, and so on. It ends with `[+] Bootstrap complete.` If you see that message with no red error lines above it, you're ready to run WheelHouse.

### What failure looks like

- **"winget is not recognized"**: Windows is missing the App Installer. Open the Microsoft Store, search for "App Installer", install it, then re-run bootstrap.
- **Network errors** ("timeout", "could not resolve host"): You're offline or behind a restrictive firewall. Reconnect and re-run.
- **"Python was not found"** after bootstrap claims it installed Python: Sometimes winget installs Python in a location the current PowerShell hasn't picked up yet. Close and reopen your terminal, then try `python --version`.
- **`uv sync` fails on a native dependency**: Install the "Visual C++ Build Tools" workload from the Visual Studio Installer, then rerun bootstrap.

### First run

When you start WheelHouse, four processes come up together:

- **The launcher** -- the thing you started. It supervises the others and restarts them if they crash.
- **The logic process** -- the brain. Routes your speech to the right action.
- **The input process** -- the fingers. Types text, clicks, presses keys.
- **The GUI process** -- the system tray icon and floating button.

You'll see the floating button appear somewhere on your screen and a WheelHouse icon in the system tray. If either is missing, see Troubleshooting.

### Microphone verification

Before you try WheelHouse, make sure Windows itself can hear your microphone:

1. Right-click the speaker icon in your taskbar -> **Sound settings**.
2. Scroll to **Input**. You should see your microphone selected and an input meter.
3. Say something -- the meter should bounce.
4. If the meter is flat, switch to a different input device or plug in a different mic.

Once Windows sees the mic, test WheelHouse itself by opening Notepad and saying "hello world" -- text should appear in about 1.5-2 seconds.

### The hotword ("x-ray")

Some WheelHouse commands are powerful or destructive (closing a window, cutting text, invoking AI). To prevent them from firing by accident during normal dictation, they require the hotword **"x-ray"** at the start of the utterance. For example:

- Say "close window" -> nothing happens (it's treated as dictation and typed as text).
- Say "x-ray close window" -> the active window closes.

Harmless commands like "undo", "copy", and "select all" don't need the hotword. The Voice Commands section below marks commands that require it with an "x-ray" prefix in the "Say this" column.

### The wake word ("computer")

If WheelHouse goes idle (no speech for a while), you can resume listening by saying **"computer"**. The wake word is separate from the hotword -- "computer" wakes WheelHouse up, "x-ray" lets you run protected commands. You can adjust the wake word sensitivity in config.toml (see Configuration Reference).

---

## Speech Engines and Accounts

### Does WheelHouse need a Google or OpenAI account?

**Short answer: No on a modern PC. Probably yes on an older PC.**

WheelHouse ships with local (offline) speech-to-text as the default. On a modern PC (roughly the last five years -- dual-core or better with 8 GB of RAM or more), the default works well without any external account and all your audio stays on your machine.

On an older or low-power PC, the CPU-only local engines produce noticeably worse accuracy and higher latency. For daily use on those machines, set up a free Google Cloud Speech-to-Text account. Google's first tier is generous and most personal users won't pay anything.

### Local vs Cloud comparison

| Aspect | Local (default) | Cloud (Google) |
|---|---|---|
| Accuracy | Good on modern hardware; weaker on CPU-only | Excellent |
| Latency | Depends on hardware (1.5-2s modern, 3-5s old) | Fast (network-dependent) |
| Privacy | All processing stays on your machine | Audio is sent to the provider |
| Cost | Free | Free tier is usually enough for personal use |
| Account needed | No | Yes (Google Cloud) |
| Works offline | Yes | No |

### How to switch

The easiest way is through the system tray: right-click the WheelHouse tray icon, pick **STT Provider**, and choose the provider you want. WheelHouse remembers your choice and uses it next time.

For advanced control (API keys, regions, custom boost words), see the `[stt]` section in Configuration Reference below.

---

## Can My Computer Handle WheelHouse?

### Minimum requirements

- Windows 10 or Windows 11
- Dual-core CPU (quad-core strongly recommended)
- 8 GB of RAM (16 GB is much more comfortable)
- An SSD, if at all possible -- WheelHouse reads a lot of small files at startup
- A working microphone

### Recommended

- 16 GB of RAM
- Modern quad-core or better CPU (Intel 8th gen / AMD Ryzen 3000 or newer)
- A dedicated microphone (any USB mic beats a typical built-in laptop mic)
- A discrete GPU if you plan to use higher-quality local STT models

### About GPUs

WheelHouse runs on CPU by default. A GPU is not required on modern hardware. With a GPU, speech recognition is noticeably faster, which matters most on older or low-power machines where CPU-only local STT is too slow for daily use.

The local speech recognition runs through **Vulkan**, which means it works on **NVIDIA, AMD, and Intel GPUs** -- there is no vendor lock-in. If your GPU supports Vulkan 1.2 or newer (almost every discrete GPU from the last 7-8 years does), WheelHouse can use it.

### What runs locally and how much memory it uses

WheelHouse does not load or host an AI model for text correction or help chat. Those features send requests to an external AI server you configure (see `[ai.server]` in Configuration Reference). The only voice-runtime component that runs locally is speech-to-text. Note: the bootstrap script also installs Ollama and pulls the `nomic-embed-text` embedding model for the code indexer -- that is a separate developer tooling component, not part of the voice or help pipeline.

| Component | Model | VRAM used |
|---|---|---|
| Speech-to-text | Whisper small.en (quantized) | ~0.4 GB |

A GPU with 1-2 GB of VRAM is plenty for the STT model. If you don't have a discrete GPU at all, WheelHouse falls back to CPU -- slower, but functional.

### "Will this be miserable on my PC?" checklist

- **If Chrome with a few tabs and Zoom run fine, WheelHouse should work.** That's the baseline.
- If your PC struggles with basic tasks (sluggish window switching, browser lag), expect noticeable delays in WheelHouse too.
- Try it -- if words take more than 3-4 seconds to appear, switch to a lighter STT model or set up a cloud STT account.

### Latency expectations

- **Modern hardware with a GPU**: roughly 1.5-2 seconds for the first word, then words flow continuously at 100-200ms intervals.
- **Modern CPU-only**: 2-3 seconds for the first word.
- **Older CPUs**: 3-5 seconds or more -- noticeable enough that dictation feels interrupted.

These are speech-to-text latencies only. Commands that call the AI server (like "x-ray fix it") have an additional round-trip to your configured `[ai.server]` on top of these figures; the extra time depends on which server and model you use.

### Tips for slow machines

- **Close heavy apps while using WheelHouse** -- especially browsers with dozens of tabs, video editors, and games.
- **Use a smaller STT model.** See the "Slow Machine Tweaks" subsection in Configuration Reference below.
- **Consider a free cloud STT account.** Google Cloud Speech-to-Text is fast, accurate, and free for most personal use.
- **Turn off features you don't use.** If you don't have Sonos speakers or a Bravia TV, disable those plugins to free resources.

---

## Voice Commands

WheelHouse turns what you say into keystrokes, text, and system actions. Most commands work without any prefix, but some powerful or destructive commands require you to start with the hotword **"x-ray"** so they don't fire accidentally while you are dictating normal text.

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
| zoom in | Zooms in (Ctrl and plus) |
| go home | Jumps the cursor to the start of the line |
| go end | Jumps the cursor to the end of the line |

### Daily Workflow Examples

**Example 1 -- Writing and cleaning up an email**

1. Dictate the body of the message normally. Sprinkle punctuation as you go: "hi team comma new paragraph the release is ready period"
2. Notice a typo two characters back: say **"backspace 2"** to rub out the last two characters, then re-dictate.
3. Finished the draft but the tone is rough: select the paragraph with **"select paragraph"**, then say **"x-ray fix it"** to send it to the configured AI server for grammar and flow cleanup.
4. Happy with the result: say **"x-ray activate outlook"** (or whatever your email app is) and **"paste"** if needed.

**Example 2 -- Editing a line of code**

1. Dictate: "def process underscore file parentheses file path colon string parentheses"
2. Cursor is at the end of the line. Say **"go home"** to jump to the beginning.
3. Say **"go right 4 words"** to skip past "def process file path".
4. Need to wrap a word in quotes: select it with **"select word"**, then say **"quotes"** to wrap it.
5. Save the file: **"x-ray save"**.

**Example 3 -- Researching something you copied**

1. Highlight a phrase on screen with the mouse.
2. Say **"copy"** to grab it.
3. Say **"x-ray browser"** to bring your browser forward.
4. Say **"paste"** into the address bar, then **"submit"** to press Enter.

### Full Voice Command Reference

#### Dictation Control

| Say this | What happens | Notes |
|---|---|---|
| push to talk mode | Switches to press-and-hold listening mode | |
| click to talk mode | Toggles listening on or off with a click | |
| x-ray wheelhouse help | Opens the Help chat window | |
| x-ray wheelhouse help [question] | Opens Help chat and asks your question | Question answered by the configured AI server |
| x-ray wheelhouse help online | Opens the configured hosted help URL in a browser (`[ai.help] gem_url`) | Requires `gem_url` to be set |
| x-ray patterns | Opens the Pattern Manager | See special commands below |

#### Text Editing

| Say this | What happens | Notes |
|---|---|---|
| backspace | Deletes one character to the left | |
| backspace [number] | Deletes that many characters to the left | e.g. "backspace 5" |
| delete | Deletes one character to the right | |
| delete [number] | Deletes that many characters to the right | |
| delete word | Deletes the entire word under the cursor | |
| new line | Inserts a line break without submitting the field | Works inline during dictation |
| new paragraph | Inserts two line breaks | Works inline during dictation |
| tab [number] | Presses Tab that many times | e.g. "tab 3" |
| shift tab | Outdents (Shift+Tab) | |
| outdent | Same as shift tab | |
| escape | Presses the Escape key | |
| submit | Presses Enter to submit the field | |
| press [keys] | Presses any key combination | e.g. "press enter", "press control alt delete", "press F5" |
| x-ray insert [text] | Inserts raw text with no capitalization or formatting | Useful for emails, tags like "TODO:" |
| item [number] | Inserts a numbered list marker like "1." | e.g. "item 1", "item 5" |

##### The "press [keys]" Command in Detail

"press [keys]" is the generic escape hatch for any keyboard shortcut. Modifiers are automatically held down first regardless of the order you say them -- so "press delete control" is equivalent to "press control delete". If any word in the phrase is unrecognized, WheelHouse falls through and dictates the whole phrase as regular text instead of pressing anything.

**Modifier keys you can say**: control (or ctrl), alt, shift, windows (or win).

**Navigation and editing keys**: enter (or return), escape, tab, backspace, delete (or del), insert, space, home, end, page up, page down, up, down, left, right, caps lock, print screen, pause.

**Function keys**: f1 through f12. If your STT hyphenates them (like "f-11"), WheelHouse handles that automatically.

**Letters and digits**: Any single letter a-z and any digit 0-9 are accepted directly. Example: "press control shift t".

**Symbols by spoken name**: backtick, tilde, semicolon, colon, slash (or forward slash), backslash (or back slash), pipe, question (or question mark), comma, period (or dot), quote (or double quote), single quote (or apostrophe), left/right bracket, left/right brace, left/right parenthesis (also accepts open/close), less than, greater than, equals (or equal), plus, minus (also hyphen or dash), underscore, hash (also hashtag or pound), at (or at sign), ampersand (or and sign), asterisk (or star), caret, percent, dollar (or dollar sign), exclamation (or bang).

**Examples**: "press control shift t", "press f5", "press alt f4", "press windows d", "press left bracket".

#### Undo and Redo

| Say this | What happens | Notes |
|---|---|---|
| undo | Undoes the last action | |
| undo [number] | Undoes multiple steps | e.g. "undo 3" |
| redo | Redoes the last undone action | |
| redo [number] | Redoes multiple steps | |

#### Clipboard Operations

| Say this | What happens | Notes |
|---|---|---|
| copy | Copies the current selection | |
| copy line | Copies the entire current line | |
| copy all | Selects everything and copies it | |
| copy screen | Starts the Windows screenshot snip tool | |
| x-ray cut | Cuts the current selection | Requires hotword for safety |
| paste | Pastes the clipboard contents | |
| x-ray replace all | Selects everything and pastes over it | Destructive -- requires hotword |

#### Selection

| Say this | What happens |
|---|---|
| select all | Selects everything in the current field |
| select word | Selects the word under the cursor |
| select line | Selects the current line |
| select paragraph | Selects the current paragraph |

#### Cursor Navigation

The "go" and "grab" commands can be combined with directions, counts, and units. "go" moves the cursor; "grab" moves and selects along the way. You can chain multiple moves in one utterance using "then".

| Say this | What happens |
|---|---|
| go home | Jumps to the start of the line |
| go end | Jumps to the end of the line |
| go top | Jumps to the top of the document |
| go bottom | Jumps to the bottom of the document |
| go left / go right | Moves one character |
| go left 5 / go right 5 | Moves five characters (numbers or spoken words like "three" also work, up to 50) |
| go right 3 words | Moves three words to the right |
| go left 2 paragraphs | Moves two paragraphs to the left |
| go start of word | Jumps to the start of the current word |
| go end of word | Jumps to the end of the current word |
| go beginning of paragraph | Jumps to the start of the current paragraph |
| go end of paragraph | Jumps to the end of the current paragraph |
| grab to end | Selects from the cursor to the end of the line |
| grab to home | Selects from the cursor to the start of the line |
| grab right 3 words | Selects three words to the right |
| grab left 5 | Selects five characters to the left |
| go home then grab to end | Chained -- jumps to start of line and then selects to end |

#### Text Formatting

All of these apply to whatever text is currently selected.

| Say this | What happens | Notes |
|---|---|---|
| uppercase | Converts selection to UPPERCASE | |
| lowercase | Converts selection to lowercase | |
| capitalize | Capitalizes the first letter | |
| title case | Converts selection to Title Case | |
| snake case | Converts selection to snake_case | |
| camel case | Converts selection to camelCase | |
| pascal case | Converts selection to PascalCase | |
| kebab case | Converts selection to kebab-case | |
| compress | Removes extra spacing from the selection | |
| x-ray bold text | Bolds the selection (Ctrl+B) | |
| x-ray italics | Italicizes the selection (Ctrl+I) | |
| x-ray underline | Underlines the selection (Ctrl+U) | |

#### Wrapping Operations

These wrap your selection in the chosen characters. If you say them without any selection, they insert an empty pair and drop your cursor inside. You can also say them followed by the text you want wrapped.

| Say this | What happens |
|---|---|
| parentheses | Wraps selection in ( ) or inserts empty ( ) |
| parentheses hello | Inserts "(hello)" |
| brackets | Wraps selection in [ ] |
| braces | Wraps selection in { } |
| angle brackets | Wraps selection in < > |
| quotes | Wraps selection in double quotes |
| single quotes | Wraps selection in single quotes |

#### Punctuation and Symbols

These work **inline during dictation** -- you do not need to pause or say them as a standalone command. Just say them as part of your sentence and WheelHouse replaces the spoken word with the symbol.

| Say this | You get |
|---|---|
| period | . |
| comma | , |
| colon | : |
| semicolon | ; |
| question mark | ? |
| exclamation point (or mark) | ! |
| apostrophe | ' |
| hyphen | - |
| dash | -- (em dash) |
| slash | / |
| backslash | \ |
| backtick | ` |
| at sign | @ |
| hashtag | # |
| dollar sign | $ |
| percent | % |
| caret sign | ^ |
| ampersand (or "and sign") | & |
| asterisk | * |
| underscore | _ |
| plus sign | + |
| equal sign | = |
| tilde | ~ |
| vertical bar | \| |
| ellipsis | ... |
| space bar | (a literal space) |

If the speech recognizer mishears "comma" as "call mom", "karma", or "kama", WheelHouse still inserts a comma. If it mishears "Claude" as "Claudia" or "clawed", it still types "Claude".

#### Application Switching

| Say this | What happens | Notes |
|---|---|---|
| x-ray browser | Brings your browser to the front | |
| x-ray editor | Brings your code editor to the front | |
| x-ray activate [app name] | Brings the named app forward | e.g. "x-ray activate outlook" |
| keyboard | Launches the on-screen virtual keyboard | |
| Windows settings | Opens the Windows Settings app | |

#### Window Management

| Say this | What happens | Notes |
|---|---|---|
| zoom in | Zooms in (Ctrl and plus) | |
| zoom out | Zooms out (Ctrl and minus) | |
| create tab | Opens a new tab (Ctrl+N) | |
| create window | Opens a new window (Ctrl+Shift+N) | |
| x-ray close window | Closes the active window (Alt+F4) | Requires hotword for safety |
| x-ray maximize | Maximizes the active window | |
| x-ray minimize | Minimizes the active window | |
| x-ray desktop | Shows the desktop (Windows+D) | |

#### WheelHouse Control and AI Assistance

| Say this | What happens | Notes |
|---|---|---|
| x-ray save | Saves the current document (Ctrl+S) | |
| x-ray find [text] | Opens find and types the search term | |
| x-ray search | Copies the selection and web searches for it | |
| x-ray replace | Opens find-and-replace (Ctrl+H) | |
| x-ray fix it | Sends the selection to the configured AI server for grammar and polish | Requires [ai.server] to be configured |
| x-ray cancel fix | Cancels an in-progress fix | |
| x-ray boost | Adds the selected text to the STT hints list | See special commands below |
| x-ray patterns | Opens the Pattern Manager | See special commands below |

#### Clicking On-Screen Controls (Numbered Overlay)

WheelHouse can click buttons, links, menu items, and other controls for you. You can click a control by its name, or have WheelHouse put a number on every clickable control and then pick one by number. The numbered overlay is handy for controls that have no obvious name to say (icon-only toolbar buttons, for example) or when several controls share the same name.

| Say this | What happens | Notes |
|---|---|---|
| x-ray click [name] | Clicks the control with that name | e.g. "x-ray click submit", "x-ray click the file menu" |
| x-ray show numbers | Puts a number on every clickable control in the front window | Numbers stay on until you hide them or click one |
| x-ray click [number] | Clicks the control labelled with that number | e.g. "x-ray click 3"; controls are numbered 1, 2, 3... from top to bottom |
| x-ray hide numbers | Removes the numbers | |

A few things worth knowing:

- **The numbers stay on screen** until you say "x-ray hide numbers" or click one of them. Say "x-ray show numbers" again at any time to repaint them, and the numbers follow you to whichever window is in front.
- **If a name is ambiguous, the numbers appear by themselves.** When you say "x-ray click [name]" and more than one control matches, WheelHouse shows numbers on just the matching controls so you can say "x-ray click [number]" to pick the one you meant.
- **Controls whose name is itself a number.** While the numbers are showing, saying a number always picks the labelled control with that number -- so a control whose real name is a digit (a calculator "7", a spreadsheet column header, a chord button) cannot be reached by saying its number. To click a control like that by its name, say "x-ray hide numbers" first, then say its name.
- **If the numbers look out of place, say "x-ray show numbers" again.** WheelHouse cannot always tell when a window scrolls or swaps its content, so after scrolling or a page change the numbers may sit in their old spots until you repaint them.

### Special Commands with Extra Explanation

**"literal [words]"**

Say "literal" followed by whatever you want to type, and WheelHouse will insert those exact words without running them through any patterns or replacements. This is the escape hatch when you need to dictate a phrase that would otherwise trigger a command.

- Example: saying "copy" normally triggers the copy command, but saying "literal copy" just types the word "copy".
- Example: "literal period" types the word "period" instead of inserting a full stop.
- Example: "literal new line" types the phrase "new line" instead of pressing Enter.

This command only takes effect when "literal" is the very first word of your utterance, so it will not interfere with normal dictation that happens to contain the word "literal" in the middle of a sentence.

**"x-ray boost"**

When the speech recognizer keeps mishearing a specific word -- usually a name, a product, a brand, or a technical term -- you can teach it to listen for that word by boosting it. The process is simple:

1. Select the problem word anywhere on screen (highlight it with the mouse or with "select word").
2. Say **"x-ray boost"**.

WheelHouse copies the selected text and sends it to your Speech-to-Text server as a new recognition hint. The hint is saved to a shared hints file on disk, so it **persists across restarts** -- you only need to boost each tricky word once. Subsequent dictation will recognize the word much more reliably.

Hint support depends on your STT provider. Google Cloud STT uses the hints list natively as speech adaptation phrases, and the local providers feed the hints into their decoder as biasing terms. Hints are limited to 100 characters per entry, so use it for individual words or short phrases rather than whole sentences.

**"x-ray patterns"**

This opens the **Pattern Manager** window, a browsable interface that lists every voice command and text replacement WheelHouse knows about. The tree on the left groups patterns by category; clicking any entry shows its details on the right -- the trigger phrase, what it does, and whether it requires the hotword.

From the Pattern Manager you can:

- **View** any pattern, including the built-in ones that ship with WheelHouse.
- **Create** new custom patterns of your own (for example, a shortcut that types your email address or opens a specific program).
- **Delete** patterns you have created yourself.

Built-in system patterns are read-only and cannot be deleted or edited, so you cannot accidentally break the core WheelHouse command set. Your custom patterns are stored separately and survive upgrades.

---

## Speech Modes

WheelHouse doesn't ask you to flip between "command mode" and "dictation mode" manually. Instead, it decides on the fly what to do with every word you say, based on the position of the word in your utterance and whether the word is known to any command pattern.

### The three behaviors

- **Command**: WheelHouse recognizes your speech as a voice command and executes it. Example: you say "undo" and it presses Ctrl+Z.
- **Dictation**: WheelHouse types what you said into the focused text field. Example: you say "dear Sarah thank you" and those words appear in your email.
- **Inline replacement**: A word mid-dictation that gets swapped for a symbol or formatting. Example: you say "period" in the middle of a sentence and WheelHouse inserts "." instead of typing the word "period".

### How WheelHouse decides

The decision depends on where the word appears in your utterance:

- **First word of an utterance**: WheelHouse checks whether the word could be the start of a command by looking it up in its pattern catalog. If yes, it buffers the word and waits for the next word or two to figure out which command you're saying. If no, it treats the word as dictation and starts typing.
- **Middle of an utterance**: WheelHouse treats the word as dictation unless it's a known replacement word (like "period" or "comma"), in which case it substitutes the symbol inline.
- **After the hotword "x-ray"**: WheelHouse forces command mode. This lets protected commands fire even mid-utterance, and ensures WheelHouse doesn't accidentally dictate what you meant as a command.

### Streaming behavior

Text appears **as you speak**, not after you stop. You'll see the first word about 1.5-2 seconds after you start talking, then subsequent words flow continuously every 100-200ms. You can start editing or speaking again before the current utterance finishes -- WheelHouse handles overlapping utterances gracefully.

### Chaining commands with "then"

You can combine multiple commands in one utterance by saying **"then"** between them:

- "go home then grab to end" -- jumps to the start of the line, then selects to the end.
- "select word then capitalize" -- selects the current word, then capitalizes it.
- "copy then x-ray browser then paste" -- copies the current selection, switches to the browser, and pastes.

"Then" is a natural way to script short workflows without memorizing combinations.

---

## Interaction Modes: Toggle vs Push-to-Talk

Speech Modes (above) describe what WheelHouse does with your words. Interaction modes control something more basic: **when WheelHouse listens at all.** There are two, and you can switch between them any time.

### Toggle mode (the default)

WheelHouse listens continuously whenever speech is enabled. You flip listening on and off with a single click on the floating button or a left-click on the system tray icon -- click once to stop listening, click again to resume. This is the right mode for hands-free use: once it's on, you never need to touch anything.

### Push-to-talk mode (PTT)

WheelHouse listens **only while you hold down** the floating button or the tray icon, like a walkie-talkie. Release, and listening stops immediately. While you're holding, WheelHouse also mutes your system audio so sound from your speakers can't leak into the microphone.

Two things to know about push-to-talk:

- **Safety release**: if a hold appears stuck (for example, the release event got lost), WheelHouse automatically stops listening after 30 seconds and restores your audio. If you do long dictations in this mode and the cut-off interrupts you, raise `ptt_safety_timeout_seconds` in the `[speech]` config section.
- It requires a hand on the mouse (or touch), so it trades away some of the hands-free benefit.

### How to switch

Three ways, any time:

- **Voice**: say **"push to talk mode"** to switch to push-to-talk, or **"click to talk mode"** to switch back to toggle.
- **Tray menu**: right-click the tray icon and check or uncheck **"Push-to-Talk Mode"**.
- **Config**: set `interaction_mode = "toggle"` or `"push_to_talk"` in the `[speech]` section of `config.toml`. This sets the mode WheelHouse starts in; the voice and tray switches change it while running.

### Which should I use?

- **Toggle** if you want hands-free control -- the primary WheelHouse use case. Set it and forget it.
- **Push-to-talk** if you're in a noisy environment (open office, TV in the room), if other people's voices keep triggering transcription, or if you only need voice input occasionally and want certainty that WheelHouse hears nothing between holds.

---

## Configuration Reference

WheelHouse's settings live in a single file: `services/wheelhouse/config.toml`. The format is TOML, which is just labeled key-value pairs with section headers in square brackets like `[speech]`. Most changes require restarting WheelHouse to take effect, and it's a good idea to make a backup copy of the file before editing.

### General Settings (top-level keys)

**REPLACEMENT_TIMEOUT_MS**
- What it does: How long WheelHouse waits (in milliseconds) for a "replace this word with that word" command to finish.
- Why change it: Increase this if replacements sometimes fail or only partially apply on your machine.
- Default: 700
- Valid values: Whole number, typically 500-2000

**COMMAND_TIMEOUT_MS**
- What it does: How long WheelHouse waits for a voice command to execute before giving up.
- Why change it: Increase this if commands feel rushed or occasionally get cut off on a slower PC.
- Default: 700
- Valid values: Whole number, typically 500-2000

**GREEDY_TIMEOUT_MS**
- What it does: How long WheelHouse waits for the rest of an utterance when the current buffer already matches a "greedy" command pattern (one that uses `.*` or `.+` to swallow the rest of what you said -- for example, `^hey Google.*$` or `\bparentheses(.*)$` to wrap the next words in parentheses). The standard COMMAND_TIMEOUT_MS / REPLACEMENT_TIMEOUT_MS of 700 ms is too short for streaming speech recognizers that deliver words one at a time with gaps longer than 700 ms between them, so a greedy pattern would race the timer and drop the trailing words into dictation.
- Why change it: Increase this if you have a slow or bursty STT provider that occasionally finalizes a greedy command before the rest of your sentence arrives. Decrease this if a long pause inside a greedy command feels too sluggish before WheelHouse acts.
- Default: 5000
- Valid values: Whole number in milliseconds, typically 2000-10000

**COMMAND_COMPLETION_WAIT_MS**
- What it does: Extra pause after a command finishes before WheelHouse is ready for the next one.
- Why change it: Increase slightly if back-to-back commands sometimes stomp on each other.
- Default: 1000
- Valid values: Whole number in milliseconds

**ENABLE_AUDIO_SUPPRESSION**
- What it does: Stops listening to your voice while your computer is actively playing audio, so TV dialogue or music doesn't get mistaken for commands.
- Why change it: Turn off if you don't play audio through your PC and detection is causing false pauses.
- Default: true
- Valid values: true / false

**ENABLE_SONOS_SUPPRESSION**
- What it does: Stops listening while a linked Sonos speaker is playing.
- Why change it: Turn off if you don't own a Sonos or don't want this behavior.
- Default: true
- Valid values: true / false

**ENABLE_IDLE_SUPPRESSION**
- What it does: Stops actively listening when you haven't used the computer for a while, to save resources and prevent stray pickups.
- Why change it: Turn off if you prefer WheelHouse to always be listening.
- Default: true
- Valid values: true / false

**SIDE_OFFSET**
- What it does: Pixel margin used when WheelHouse snaps or aligns windows against screen edges.
- Why change it: Increase if snapped windows feel too close to the edge of your display.
- Default: 10
- Valid values: Whole number of pixels

**BRIGHTNESS_INCREMENT**
- What it does: How much brightness changes each time you say "brighter" or "dimmer."
- Why change it: Raise for bigger jumps per command, lower for finer control.
- Default: 1.0
- Valid values: Decimal number (a sensible range is 0.5 to 5.0)

**VOLUME_INCREMENT**
- What it does: How much volume changes per "louder" / "quieter" command.
- Why change it: Raise for bigger jumps, lower for finer control.
- Default: 0.5
- Valid values: Decimal number

**FLOATING_BUTTON_SIZE**
- What it does: Size in pixels of the small floating WheelHouse status button on your screen.
- Why change it: Increase if the button is too small to see, decrease if it feels intrusive.
- Default: 30
- Valid values: Whole number of pixels

**FLOATING_BUTTON_POS**
- What it does: Screen position of the floating button as `[x, y]`. Negative numbers anchor to the right/bottom edge.
- Why change it: Adjust if the button ends up somewhere awkward on your display layout.
- Default: [-11, -13]
- Valid values: Two whole numbers in brackets

**FLOATING_BUTTON_VISIBLE**
- What it does: Shows or hides the floating status button entirely.
- Why change it: Turn off if you'd rather rely on the system tray icon only.
- Default: true
- Valid values: true / false

**SPEECH_ENABLED_ON_STARTUP**
- What it does: Whether WheelHouse starts listening immediately when it launches, or waits for you to turn listening on.
- Why change it: Set to false if you prefer to start each session manually.
- Default: true
- Valid values: true / false

**SHOW_SPEECH_PULSE**
- What it does: Shows a subtle pulsing visual cue on the floating button while listening.
- Why change it: Turn off if you find the animation distracting.
- Default: true
- Valid values: true / false

**GEMINI_API_KEY**
- What it does: Legacy key retained for compatibility. The active text-correction path no longer uses this key; text correction runs through `[ai.server]` (OpenAI-compatible). To use Gemini for text correction, point `[ai.server] base_url` at Gemini's OpenAI-compatible endpoint (`https://generativelanguage.googleapis.com/v1beta/openai/`) and set `[ai.server] api_key` to your Google AI Studio key.
- Why change it: Leave empty unless a specific integration you have installed explicitly reads it.
- Default: empty
- Valid values: A key string from Google AI Studio

**GEMINI_MODEL_NAME**
- What it does: Legacy setting retained for compatibility with older integrations. The active text-correction and help-chat paths do not read this variable; they use `[ai.server] model` instead. To select a Gemini model, set `[ai.server] model` (e.g. `gemini-2.5-flash`).
- Why change it: Leave empty unless a specific legacy integration you have installed explicitly reads it.
- Default: "gemini-2.5-flash" (the value shipped in config.toml; legacy only, not read by the active text-correction or help-chat paths -- set `[ai.server] model` instead)
- Valid values: Any valid Gemini model name (legacy use only)

**SPATIAL_SOUND_EXEC**
- What it does: Path to a small helper tool WheelHouse uses to switch Windows spatial audio modes on and off.
- Why change it: Update only if you've moved or reinstalled the helper tool.
- Default: points to the SoundVolumeCommandLine utility
- Valid values: Full path to the `svcl.exe` executable

**SPATIAL_SOUND_FORMAT**
- What it does: Which spatial sound mode WheelHouse switches to when enabling spatial audio.
- Why change it: Change if you use Windows Sonic or a different Dolby format.
- Default: "Dolby Atmos for home theater"
- Valid values: "Windows Sonic for Headphones", "Dolby Atmos for Headphones", "Dolby Atmos for home theater", or "DTS Headphone:X"

---

### Brightness Coordinator (`[brightness_coordinator]`)

**software_dimmer**
- What it does: Which dimming method WheelHouse uses once your monitor reaches its lowest hardware brightness. `gamma_dimmer` adjusts the display's color ramp, while `software_dimmer` overlays a dark layer on the screen.
- Why change it: Switch to `software_dimmer` if gamma dimming doesn't work on your GPU or looks odd.
- Default: gamma_dimmer
- Valid values: "gamma_dimmer" or "software_dimmer"

**unwinding_threshold**
- What it does: Controls how aggressively WheelHouse "unwinds" software dimming back into real monitor brightness when you ask for more light. Higher numbers mean it prefers software changes for longer before touching the hardware.
- Why change it: Lower it if brightening feels sluggish. Raise it if the screen flickers between methods too often.
- Default: 10
- Valid values: Whole number, typically 5-20

**flux_transition_percent**
- What it does: The brightness "step size" WheelHouse uses when gradually easing between levels, so changes feel smooth instead of jumpy.
- Why change it: Raise for faster, snappier transitions. Lower for smoother, more gradual fades.
- Default: 2
- Valid values: Whole number percentage

**flux_dim_hotkey** / **flux_brighten_hotkey**
- What it does: Global keyboard shortcuts that dim or brighten the display without using voice.
- Why change it: Remap if these conflict with another app's shortcuts.
- Default: `["alt", "pagedown"]` and `["alt", "pageup"]`
- Valid values: A list of key names in brackets, for example `["ctrl", "shift", "f1"]`

---

### Speech Settings (`[speech]`)

**notify_on_revision**
- What it does: Pops up a small notification when WheelHouse automatically revises a word it misheard.
- Why change it: Turn on if you want to see exactly what WheelHouse is correcting on the fly.
- Default: false
- Valid values: true / false

**interaction_mode**
- What it does: How you control listening. `toggle` means say a hotword or click to start/stop. `push_to_talk` means hold a key while speaking, like a walkie-talkie.
- Why change it: Switch to push-to-talk in noisy environments where you want precise control.
- Default: "toggle"
- Valid values: "toggle" or "push_to_talk"

**ptt_safety_timeout_seconds**
- What it does: In push-to-talk mode, if your PTT key appears stuck down for this long, WheelHouse automatically releases it to prevent runaway listening.
- Why change it: Increase if you do long dictations in push-to-talk mode and the safety cut-off kicks in too early.
- Default: 30
- Valid values: Whole number of seconds

---

### Wake Word (`[wake_word]`)

**enabled**
- What it does: Turns the wake-word feature on or off. When on, you can say the keyword to start listening after an idle period.
- Why change it: Turn off if you never want a hands-free trigger.
- Default: true
- Valid values: true / false

**keyword**
- What it does: The word WheelHouse listens for to wake itself up.
- Why change it: Pick a word less likely to come up in normal speech around your computer.
- Default: "computer"
- Valid values: One of the supported wake-word models (check available models in your WheelHouse installation)

**sensitivity**
- What it does: How easily WheelHouse accepts a possible wake-word match. Higher = triggers more easily but with more false alarms.
- Why change it: Raise if the wake word is hard to trigger; lower if it goes off on its own too often.
- Default: 0.5
- Valid values: Decimal between 0.0 and 1.0

**mode**
- What it does: When the wake word is active. `idle_recovery` means it only listens for the wake word when WheelHouse has gone idle.
- Why change it: Generally leave as-is unless advised.
- Default: "idle_recovery"
- Valid values: "idle_recovery" or "always"

---

### UI Action Timing (`[ui_actions.timing]`)

These are tiny delays (in milliseconds or seconds) that coordinate copy, paste, and selection behind the scenes. **Most users should never touch these** -- they're only worth adjusting if you're seeing specific issues like dropped characters or failed pastes.

**clipboard_verification_timeout_ms** (default 250) -- How long to wait for the clipboard to confirm it has the text WheelHouse just copied to it. Increase if dictation sometimes fails to insert on slow machines.

**clipboard_operation_delay_ms** (default 50) -- A short pause WheelHouse takes between clipboard actions so Windows can catch up. Increase if clipboard-based inserts feel unreliable.

**selection_clear_delay_ms** (default 20) -- Pause after clearing a text selection. Rarely needs changing.

**context_gather_delay_ms** (default 10) -- Brief pause before reading the surrounding text so WheelHouse knows what you're editing. Rarely needs changing.

**post_paste_delay_ms** (default 30) -- Pause after a paste completes. Increase if pastes sometimes lose their last character or two.

**utterance_clipboard_timeout_seconds** (default 60) -- How long an utterance's clipboard snapshot stays available for edit/undo follow-ups. Lengthen only if you often come back to edit a dictation many seconds later.

---

### STT (Speech-to-Text) (`[stt]` and subsections)

**last_provider**
- What it does: Remembers which speech recognition engine was last used, so WheelHouse starts up with the same one next time. Usually you change this through the system tray menu, not by editing the file.
- Why change it: Edit only if you need to force a specific provider on startup, or to recover after a crash.
- Default: "distil_medium_en"
- Valid values: The name of any installed STT provider (for example `distil_medium_en`, `sherpa_offline_parakeet_stt_server`, `google_stt`)

**stt.google.boost_words**
- What it does: A list of words Google's recognizer should be extra-biased toward hearing. Useful for names, jargon, or brand names that are often misheard.
- Why change it: Add technical terms, people's names, or app names you dictate often.
- Default: empty list
- Valid values: A list of words in brackets, like `["WheelHouse", "Claude", "tailwind"]`

---

### AI Settings (`[ai]` and subsections)

**enabled**
- What it does: Master switch for all AI features (text correction and the help chat).
- Why change it: Turn off to disable AI entirely and save resources.
- Default: true
- Valid values: true / false

**knowledge_base**
- What it does: Path to the help documentation the AI consults when answering "how do I..." questions about WheelHouse.
- Why change it: Point at a different file if you want the help chat to use customized documentation.
- Default: "knowledge/wheelhouse_help.md"
- Valid values: Path to a markdown file

WheelHouse does not load or host an AI model itself. The text-correction and help-chat features are thin clients that talk to an external AI server speaking the standard OpenAI API. You point WheelHouse at a server you control -- a local one running on your own machine or network, or a hosted one -- and WheelHouse sends it requests. If no server is configured or reachable, the AI features simply stay off and everything else keeps working.

#### `[ai.server]` (which AI server to talk to)

**base_url**
- What it does: The web address of the AI server's OpenAI-style API. For a local server (like Ollama running on your own PC) this is usually something like `http://localhost:11434/v1`. For a hosted service, use the address that service gives you.
- Why change it: Set this to point WheelHouse at your AI server. Leave it empty to turn the AI features off entirely.
- Default: "http://localhost:11434/v1"
- Valid values: A full http or https URL for an OpenAI-compatible API root -- usually ending in `/v1` (e.g. `http://localhost:11434/v1`), though some hosted endpoints use a different path, such as Gemini's `https://generativelanguage.googleapis.com/v1beta/openai/`; or empty to disable AI. A root that does not end in `/v1` still works (WheelHouse only skips the local model-list refresh and logs a notice).

**model**
- What it does: The name of the model you want the server to use for your requests. This must be a model that server actually has available.
- Why change it: Switch to a different model your server offers -- a smaller one for faster answers, a larger one for higher quality.
- Default: empty (you fill in the name your server serves)
- Valid values: Any model name your configured server recognizes

**kind**
- What it does: Tells WheelHouse whether the server is on your own machine or network (`local`) or a hosted cloud endpoint (`cloud`). This affects two behaviors: `local` enables live model-list refresh from the server so WheelHouse always shows what models are available; `cloud` skips that refresh and uses the model name you configured. It is also used to frame the privacy tradeoff -- a local server keeps your text on your own hardware.
- Why change it: Set to `local` for a server you run yourself (e.g. Ollama on localhost). Set to `cloud` for an OpenAI-compatible hosted service or gateway (e.g. OpenAI, or Gemini/Anthropic via an OpenAI-compatible gateway such as OpenRouter). WheelHouse always uses the OpenAI-compatible API; native vendor APIs that do not expose that interface will not work.
- Default: "local"
- Valid values: "local" or "cloud"

**api_key**
- What it does: The credential WheelHouse sends to the server, for servers that require one.
- Why change it: Fill this in if your AI server or hosted service needs an API key. A local server usually needs none, so leave it empty.
- Default: empty
- Valid values: The API key string your server requires, or empty

**timeout_s**
- What it does: How many seconds WheelHouse waits for the AI server to answer before giving up on a request.
- Why change it: Raise it if your server is slow to respond on the first request; lower it if you'd rather fail fast when the server is unreachable.
- Default: 30
- Valid values: Whole number of seconds

#### `[ai.help]`

**gem_url**
- What it does: An optional URL for a hosted help page (such as a custom GPT or Gem). When set, the `x-ray wheelhouse help online` command opens this URL in your browser. In-app help chat still uses the configured `[ai.server]` regardless of this setting.
- Why change it: Set it if you've published a hosted help assistant and want the help-online command to open it directly.
- Default: empty
- Valid values: A full URL, or empty (the help-online command will say "Online help is not configured" if this is empty)

**max_response_tokens**
- What it does: Caps how long a single help answer can be. More tokens = potentially longer answers but slower responses.
- Why change it: Lower for snappier replies. Raise if the AI keeps getting cut off mid-explanation.
- Default: 800
- Valid values: Whole number, typically 200-2000

---

### Terminal (`[terminal]`)

**submit_delay_ms**
- What it does: Tiny pause after dictated text lands in a terminal, before WheelHouse presses Enter for you.
- Why change it: Increase if your terminal sometimes submits before the full text has appeared.
- Default: 100
- Valid values: Whole number of milliseconds

---

### Slow Machine Tweaks

If WheelHouse feels sluggish, or commands and dictation are slightly unreliable on an older or low-power PC, try these targeted adjustments. Change one at a time and restart WheelHouse after each so you can tell what helped.

1. **Give commands more breathing room.** Raise `COMMAND_TIMEOUT_MS` from 700 to around 1200, and `REPLACEMENT_TIMEOUT_MS` from 700 to 1000. This prevents WheelHouse from declaring a command "failed" when it's really just slow.

2. **Use a lighter speech-to-text model.** If your machine lacks an NVIDIA 4GB+ GPU, set `[stt] last_provider` to `sherpa_offline_parakeet_stt_server` (CPU-only Parakeet v3) or `google_stt` (cloud). These are lighter than the GPU `distil_medium_en` default.

3. **Move the AI off your machine.** Because WheelHouse is a thin client, the AI does not run on your PC -- it runs wherever `[ai.server] base_url` points. If your computer is struggling, point `base_url` at a faster server (a beefier machine on your network or a hosted service) so your PC only handles speech, not the model.

4. **Pick a lighter model on the server.** Change `[ai.server] model` to a smaller model your server offers. Smaller models answer faster and use less memory on whatever machine is hosting them.

5. **Turn the AI off entirely.** If you don't need text correction or the help chat, set `[ai] enabled = false` (or clear `[ai.server] base_url`). Everything else -- dictation, commands, plugins -- keeps working, and your PC has more headroom.

6. **Relax paste timing.** If dictated text sometimes loses a character or two, raise `[ui_actions.timing] post_paste_delay_ms` from 30 to 50 or 75, and `clipboard_verification_timeout_ms` from 250 to 500.

7. **Turn off audio suppression** if you don't use Sonos and your PC rarely plays audio: set `ENABLE_AUDIO_SUPPRESSION = false` and `ENABLE_SONOS_SUPPRESSION = false`. This removes a background monitoring task.

8. **Make the wake word easier to trigger.** If "computer" isn't reliably activating WheelHouse, raise `[wake_word] sensitivity` from 0.5 to 0.7. If you get too many false triggers instead, lower it to 0.3.

9. **Extend the PTT safety timeout.** If you use push-to-talk for long dictations, raise `[speech] ptt_safety_timeout_seconds` from 30 to 60 or 90 so the safety release doesn't cut you off.

---

## Plugins

Plugins extend WheelHouse with optional integrations for external hardware and services like TVs, speakers, laptop displays, and Windows system features. Each plugin lives in its own `[plugins.*]` section of `config.toml` and has an `enabled = true` or `enabled = false` flag so you can turn it on or off without removing configuration. Plugins communicate with the rest of WheelHouse through an internal event bus, which means they can react to voice commands, mouse-wheel input, and system events without tight coupling. If a plugin's hardware is missing or offline, WheelHouse keeps running -- the plugin simply reports itself as unhealthy and retries in the background.

---

### Internal Panel

**What it does**: Controls the brightness of a Windows laptop's built-in screen so you can adjust it with voice or the mouse wheel.

**Enable/disable**: Set `plugins.internal_panel.enabled` to `true` or `false` in config.toml.

**Configuration**: None beyond enable/disable. WMI settings are auto-detected.

**What it connects to**: The laptop's built-in display via the Windows WMI brightness API (`WmiMonitorBrightness`).

**When to enable**: On a Windows laptop where you want voice or mouse-wheel control of the internal screen's brightness. On a desktop PC (no internal panel), the plugin gracefully does nothing, so it's safe to leave enabled.

---

### Sonos Speaker Control

**What it does**: Adjusts Sonos speaker volume by voice or mouse wheel and tells WheelHouse to pause listening while music is playing so audio doesn't get mistranscribed as commands.

**Enable/disable**: Set `plugins.sonos.enabled` to `true` or `false`.

**Configuration**:
- `polling_interval` -- How often (in seconds) the plugin checks whether your Sonos is playing. Default: `2`. Lower values mean faster response to playback changes but more network traffic.
- `speaker_ip` -- Optional. WheelHouse auto-discovers Sonos speakers on your network; set this only if discovery fails or you have several speakers and want a specific one. To find the address, open the Sonos app and go to Settings -> System -> About My System, then note the speaker's IP (for example `192.168.1.100`).

**What it connects to**: Sonos speakers on your local network via Sonos' UPnP API (no Sonos cloud account required).

**When to enable**: If you own Sonos speakers and want WheelHouse to automatically suppress voice recognition while they're playing, plus control their volume with your existing WheelHouse input methods.

---

### System Volume

**What it does**: Controls the Windows system volume (the same volume your taskbar speaker icon controls) in response to voice commands and mouse-wheel input.

**Enable/disable**: Set `plugins.system_volume.enabled` to `true` or `false`. Note: this plugin and the Sonos plugin both handle volume commands -- typically you'd pick one based on which speakers you use.

**Configuration**:
- `device_type` -- Which audio device to control. `"default"` uses the current Windows default playback device; `"communications"` uses the communications default; or specify a device name directly.
- `volume_step_db` -- How many decibels to change per volume step. Default: `1.5`.
- `min_volume_db` -- Lower bound in dB. Default: `-96.0` (effectively muted).
- `max_volume_db` -- Upper bound in dB. Default: `0.0` (Windows maximum).

**What it connects to**: Windows Core Audio APIs via the pycaw library (fully local, no network).

**When to enable**: Any Windows PC where you want voice or mouse-wheel control of the system volume. This is the right choice for most users who don't own Sonos speakers.

---

### Sony Bravia TV

**What it does**: Integrates a Sony Bravia TV into WheelHouse's brightness control system so voice brightness commands can dim or brighten the TV when it's being used as a monitor.

**Enable/disable**: Set `plugins.bravia.enabled` to `true` or `false`.

**Configuration**:
- `ip_address` -- Set to your TV's IP address on the local network.
- `psk` -- Set to the pre-shared key you configured in your TV's network settings (Settings -> Network -> Home Network -> IP Control -> Pre-Shared Key).
- `device_name` -- A friendly name for the TV, used in logs and status displays. Set this to anything you'll recognize (for example, `"Living Room TV"`).

**What it connects to**: A Sony Bravia TV on your local network via Sony's IP Control REST API.

**When to enable**: If you use a Sony Bravia TV as a computer display and want WheelHouse to dim it as part of voice brightness commands, alongside your other displays.

---

### Idle Monitor

**What it does**: Watches for inactivity on your keyboard and mouse, and automatically pauses speech transcription after you've been idle for a while so WheelHouse isn't listening to an empty room. Listening resumes when you come back.

**Enable/disable**: Set `plugins.idle_monitor.enabled` to `true` or `false`.

**Configuration**:
- `idle_timeout_minutes` -- How many minutes of no keyboard or mouse activity before WheelHouse considers you "idle" and pauses listening. Default: `10`.
- `polling_interval_seconds` -- How often (in seconds) to check for activity. Default: `4`. Lower values detect idleness faster but use marginally more CPU.

**What it connects to**: The Windows `GetLastInputInfo` API -- fully local, no network or external hardware.

**When to enable**: Almost always a good idea. It prevents accidental transcription when you step away, and it conserves STT costs if you're using a paid cloud transcription provider.

---

### Window Positioning

**What it does**: Automatically moves specific windows (by default, the Windows On-Screen Keyboard) out of the way when they would cover the window you're currently typing in. Designed for voice and accessibility users who rely on the on-screen keyboard.

**Enable/disable**: Set `plugins.window_positioning.enabled` to `true` or `false`.

**Configuration**:
- `target_window_names` -- A list of window titles the plugin will reposition. Defaults to `["On-Screen Keyboard", "osk"]`. Add the title of any other window you want WheelHouse to auto-move.
- `move_cooldown_seconds` -- Minimum time between automatic moves, to prevent the window from jittering if focus changes rapidly. Default: `0.5`.
- `clearance_gap_pixels` -- How many pixels of empty space to leave between the repositioned window and the active window's edge. Default: `5`.
- `ignore_window_titles` -- A list of window titles that should never trigger repositioning (for example, the taskbar or Start menu).
- `ignore_window_classes` -- Same idea, but matched by the window's internal class name (useful for system windows that don't have a user-visible title).

**What it connects to**: Windows accessibility event hooks -- fully local, no network.

**When to enable**: If you use the Windows On-Screen Keyboard for touch or accessibility input and you're tired of it covering whatever you're trying to type into.

---

### Software Dimmer (disabled by default)

A fallback display dimmer that uses a translucent software overlay instead of hardware gamma ramps. Most users should leave this off -- the gamma-based dimmer used by the Brightness Coordinator is smoother, affects the whole display uniformly, and has no visual artifacts. Turn this on only if gamma dimming doesn't work correctly on your graphics hardware.

---

### Choosing and Configuring Plugins

Three rules cover almost every setup decision:

1. **Enable only ONE volume plugin** -- Sonos OR System Volume, not both. They both answer volume commands and will conflict. System Volume is the right choice for most users; Sonos only if you own Sonos speakers.
2. **Brightness plugins can combine.** Enable several for multi-display setups (for example a laptop panel plus a TV): WheelHouse adjusts all available hardware together, and when hardware hits its limit (fully bright or fully dim), further adjustment cascades to software dimming automatically. No priority configuration is needed.
3. **Restart WheelHouse after any plugin change.** Plugins are discovered and initialized at startup.

Example configurations (replace the addresses with your own devices'):

```toml
# Most users: Windows volume + laptop screen
[plugins.system_volume]
enabled = true

[plugins.internal_panel]
enabled = true

[plugins.sonos]
enabled = false
```

```toml
# Sonos speakers + Sony TV as monitor
[plugins.sonos]
enabled = true
speaker_ip = "192.168.1.100"   # optional; auto-discovery is the default

[plugins.system_volume]
enabled = false

[plugins.bravia]
enabled = true
ip_address = "192.168.1.101"   # your TV's IP
psk = "your_psk_here"          # from the TV's IP Control settings
device_name = "Living Room TV"
```

**Plugin troubleshooting basics**: confirm `enabled = true` and restart; check the log's plugin initialization lines ("Plugin available" vs "Plugin not available" shows what was detected); for network plugins (Sonos, Bravia) verify the device's IP is reachable from the PC and, for Bravia, that IP Control is enabled on the TV and the pre-shared key matches; if nothing responds to the mouse wheel, make sure you are scrolling in the correct zone (volume vs brightness) and that at least one plugin for that control type is enabled.

---

## Troubleshooting

### First-Time Setup Checklist

If something isn't working, walk through these five checks in order. Stop at the first failure and jump to the matching section below.

1. **Did bootstrap complete without red error lines?** If not, see "Bootstrap script failures."
2. **Do Windows Sound settings show microphone input?** Right-click the speaker icon -> Sound settings -> Input. Speak -- does the meter bounce? If not, see "Microphone not detected."
3. **Is the system tray icon visible and green?** If grey or missing, see "WheelHouse doesn't start / tray icon missing."
4. **Test: open Notepad, say "hello" -- does text appear?** If not, see "Dictation not appearing in text fields."
5. **Test: say "undo" -- does text disappear?** If not, see "Commands not recognized."

---

### Common Problems and Solutions

**Microphone not detected**

- *What you see*: WheelHouse starts but no text appears when you speak. Windows Sound settings show no input activity.
- *Likely cause*: Wrong default input device, or a USB mic is plugged in but not selected.
- *Try*: Right-click the taskbar speaker icon -> Sound settings -> Input -> pick the correct device. Close and restart WheelHouse.

**Dictation not appearing in text fields**

- *What you see*: You speak, you can hear that STT is processing (possibly a pulse on the floating button), but nothing appears in the app you're typing into.
- *Likely cause*: The target window isn't actually focused, or the app uses a non-standard text control that WheelHouse's insertion strategies don't recognize.
- *Try*: Click directly in the text field first. Try a simpler app (Notepad) to confirm WheelHouse itself is working. If Notepad works and the problem app doesn't, the app is probably using a text control that falls back to clipboard paste -- make sure nothing else is watching your clipboard.

**Commands not recognized**

- *What you see*: You say "undo" but nothing happens -- the word gets typed as regular text instead.
- *Likely cause*: Either the STT engine misheard you (said "undue" or "and do") or a custom pattern is intercepting the word.
- *Try*: Speak more deliberately and check the WheelHouse log for what the STT actually heard. If the word is consistently misrecognized, select a clean copy of it anywhere on screen and say "x-ray boost" to add it to the STT hints list.

**Hotword-protected commands not firing**

- *What you see*: You say "close window" and it types "close window" into your document instead of closing anything.
- *Likely cause*: That command requires the "x-ray" hotword prefix. Say "x-ray close window" instead. (Hotword protection is intentional -- it prevents destructive commands from firing accidentally during normal dictation.)

**WheelHouse doesn't start / tray icon missing**

- *What you see*: You run the launcher and nothing appears, or the tray icon is there but greyed out.
- *Likely cause*: One of the four child processes crashed during startup. Most commonly: a port conflict (the speech WebSocket couldn't bind), a missing model file, or a uv environment mismatch after a dependency change.
- *Try*: Check the WheelHouse log file for the first red error. Re-run `uv sync` in the `services/wheelhouse` directory. If a port conflict is the issue, the log will tell you which port; change `SPEECH_WEBSOCKET_HOST` in config.toml or shut down whatever else is using that port.

**STT provider won't connect**

- *What you see*: The tray icon shows STT as disconnected, or you see "waiting for STT" in the UI.
- *Likely cause*: The STT subprocess failed to start -- wrong model path, missing API key for cloud providers, or the local model won't fit in memory.
- *Try*: Switch to a lighter STT provider from the tray menu (try `sherpa_offline_parakeet_stt_server` for CPU, or `google_stt` for cloud). If you're using a cloud provider, double-check the API key and region in config.toml.

**Input Process crashes with heap corruption** *(known issue, fixed)*

- *What you see*: On older builds, the Input Process could terminate unexpectedly during sustained dictation with exit code 3221226356 (heap corruption). The launcher would detect the crash and shut down.
- *Cause*: The clipboard restore logic wrote stale binary clipboard formats that Windows rejected, corrupting the heap.
- *Status*: Fixed. The fix limits clipboard save/restore to text-only formats (CF_TEXT, CF_UNICODETEXT, CF_OEMTEXT). If you see this crash on a current build, update WheelHouse.

**AudioMonitor flooding the log with "Element not found" errors** *(known issue, fixed)*

- *What you see*: On older builds, after extended idle periods the log could fill with `Failed to check audio status: (-2147023728, Element not found)` entries at roughly 100ms intervals.
- *Cause*: Windows audio sessions and endpoints change state during idle, which returned a COM error that the monitor didn't handle gracefully.
- *Status*: Fixed. The audio monitor now logs a single warning on first failure, retries silently for up to 60 seconds, then escalates. If you see this on a current build, update WheelHouse.

**AI features (help chat or text correction) do nothing or time out**

- *What you see*: The help chat never answers, or dictated text isn't being cleaned up, even though `[ai] enabled = true`.
- *Cause*: WheelHouse is a thin client and does not run the AI itself -- it sends requests to the server named in `[ai.server] base_url`. If that server is missing, unreachable, slow, or serving a model name WheelHouse didn't ask for, the AI features quietly stay off while the rest of WheelHouse keeps working.
- *Try (in order)*:
  1. Confirm `[ai] enabled = true` and that `[ai.server] base_url` is filled in (for a local Ollama server it's usually `http://localhost:11434/v1`). An empty `base_url` turns AI off on purpose.
  2. Make sure the AI server is actually running and reachable at that address, and that `[ai.server] model` is a model that server has available.
  3. If the server is slow on its first request, raise `[ai.server] timeout_s` so WheelHouse waits longer before giving up.
  4. For a hosted server that needs credentials, check that `[ai.server] api_key` is set correctly.
- *Reassurance*: An unreachable AI server never breaks WheelHouse. Dictation, voice commands, navigation, and plugins all keep working with AI off.

**Terminal editor dictation loses its last character**

- *What you see*: Dictated text in the terminal editor window is missing a character or two at the end.
- *Likely cause*: The post-paste delay is too short for your terminal to finish processing the incoming text before WheelHouse moves on.
- *Try*: Increase `post_paste_delay_ms` in `[ui_actions.timing]` from 30 to 50 or 75.

**IPC timeout between processes**

- *What you see*: A command partially runs and then hangs, or the tray icon turns red.
- *Likely cause*: The Input Process is unresponsive (busy with a slow UI automation call) and the Logic Process timed out waiting for a response.
- *Try*: Restart WheelHouse. If it happens repeatedly in the same app, the app's UI Automation support is probably the culprit -- try using a simpler dictation target to isolate the problem.

**Bootstrap script failures**

- *What you see*: The bootstrap script exits with an error partway through.
- *Common causes and fixes*:
  - **"winget not found"**: Install App Installer from the Microsoft Store.
  - **"Python 3.12 not found" after install claims success**: Close PowerShell, open a fresh one, and re-run the script.
  - **Network timeout during model download**: Rerun the script on a faster connection. The script is idempotent -- running it again from the start is safe.
  - **`uv sync` fails on a native dependency**: Install the "Visual C++ Build Tools" workload from the Visual Studio Installer, then rerun bootstrap.

---

## Getting Help

If you can't find the answer here, you can reach the WheelHouse developer at the WheelHouse GitHub page: https://github.com/wheelhouse-project/WheelHouse (open an issue or start a discussion). This project is actively under development, so please include your WheelHouse version (see the footer of this document) and a clear description of what you tried.

---

Generated: 2026-04-07 (Interaction Modes section added 2026-07-05, wh-g1y)
WheelHouse version: 1.0.0
