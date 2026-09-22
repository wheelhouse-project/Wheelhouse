# Changelog

All notable changes to Wheelhouse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-21

### Added

- You can now say "erase" wherever you can say "delete". For example,
  "erase word", "erase line", "erase paragraph" and "erase next three
  words" work the same as the "delete" commands. "backspace" is unchanged.
- Help now shows a short window before your browser opens. It explains
  that the Wheelhouse Assistant runs inside ChatGPT and needs a ChatGPT
  account; a free account works. Only the Assistant button opens the
  browser. Cancel or Escape closes the window and opens nothing. Tick
  "Do not show this again" and choose Assistant to go straight to the
  browser next time. If your browser does not start, Wheelhouse now
  tells you.

### Changed

- If your speech engine does not use hint phrases, saying "boost" now
  types the word "boost". Wheelhouse no longer saves a hint that the
  engine would ignore. The Pattern Manager "Try it" field gives the same
  answer. The help document names the engines that use hint phrases.
- The help document now explains that the Windows microphone setting
  "Audio enhancements" can remove the echo canceller, and how to change
  it.

### Fixed

- The floating button no longer gets lost off screen. If less than half
  of the button is on a screen, Wheelhouse moves it back onto the nearest
  screen at start-up, after you change your displays, and after you drag
  it past an edge.
- Setup wizard text is no longer cut off on displays scaled above 100
  percent. The options on the "AI helper (optional)" page show in full,
  and the "still working, please wait" note now has its own line.
- On Windows 10 computers with an old Microsoft Visual C++ runtime, the
  Parakeet speech engine no longer crashes. The installer now installs
  the runtime, waits for it to finish, and tells you if you declined the
  Windows permission prompt.
- A dictated word is no longer lost when the place you are typing into
  closes at the moment of typing, for example a browser menu that shuts.
  Wheelhouse looks once more for that place in the same program and
  types the word there. It never types the same word twice, and it never
  types into a different program.
- When one word fails in a longer sentence, Wheelhouse can still correct
  the rest of the sentence when the speech engine revises what you said.
- If you turn listening back on while a video plays, Wheelhouse now
  pauses listening again when the video's sound continues after a short
  quiet moment. Before this fix, the video's words were typed until the
  video ended.

### Known issues

- Over Remote Desktop, when the microphone comes from the remote
  connection, speech recognition can stop as soon as it starts. The log
  shows "AudioGraph creation failed: status=3". Until this is fixed, run
  Wheelhouse on the computer you sit at.
- On a computer with less memory than the installer requires, the
  installer stops before it asks for a speech engine. Its message
  suggests the Google Cloud engine, but the installer does not offer that
  engine on such a computer.
- In Visual Studio Code, the rows of an open menu such as File get no
  numbers, and a click on a row by its name is refused. Visual Studio Code
  reports those rows as off-screen, so Wheelhouse does not find them.

## [1.0.8] - 2026-09-19

### Added

- Dictation now writes numbers, dates, money amounts, times and street
  addresses as digits. The spoken words twenty dollars become $20, and
  January third becomes January 3rd. Spoken commas are handled too, and
  they start at ten thousand: fifteen thousand becomes 15,000, while one
  thousand two hundred stays 1200.
- You can click a numbered badge by speaking its number alone, once the
  badges are on screen. Number words, digits and multi-word numbers all
  work: with 112 badges showing, the words one twelve click badge 112.
- New keyboard commands move the cursor without typing anything: "press
  up arrow", "press down arrow", "press left arrow" and "press right
  arrow". Saying "keyboard" opens or closes the Windows On-Screen
  Keyboard.
- Three more ways to reach a program by voice: "x-ray switch to notepad",
  "x-ray go to notepad" and "x-ray show notepad" now do what "x-ray
  activate notepad" already did, and each one starts the program when no
  window is open. If starting the program takes more than eight seconds,
  Wheelhouse tells you instead of leaving you waiting. Four programs can
  start at once; a fifth is refused with a message asking you to try
  again.
- When a key press, a keyboard shortcut or typing fails, Wheelhouse now
  shows a notice instead of doing nothing. The notice names the key or
  the shortcut, says when Wheelhouse does not know the key name, and
  reports how many characters were typed before typing stopped.
- The floating push-to-talk button now shows two new colours. Amber means
  Wheelhouse has your hold and is waiting for confirmation. Purple with an
  exclamation mark means something is blocking the hold, and the notice
  says what.
- Right-clicking the floating button shows a short description under each
  menu item when you hover over it.
- If the microphone cannot be opened, Wheelhouse now says so instead of
  reporting that transcription is ready. The ready notice also says
  whether word boosting from your hint phrases is switched on, and why it
  is off when it is off.
- A new mouse grid clicks anywhere on screen by voice. Say "show grid"
  to divide the screen into nine numbered cells, say a cell number to
  divide that cell again, and repeat until the cell is where you want
  it. Then say "click", "right click" or "double click" to click in
  that cell, or "move here" to move the pointer there without
  clicking. To drag, say "mark" at the first point, choose a second
  cell, and say "drag". Say "grid next screen" to move the grid to
  another monitor, and "hide grid" to close it.
- Right click and double click now work on a control you name and on a
  numbered badge as well.
- A new guided session teaches Wheelhouse how you sound, so the
  Distil-Whisper engine stops missing short words spoken alone, such as
  "comma". Say "learn my voice" or "calibrate my voice", or right-click
  the floating button or the tray icon, open "STT Provider" and choose
  "Teach WheelHouse your voice...". Only the Distil-Whisper engine uses
  it; with another engine the window says so and changes nothing.
- Wheelhouse now checks the installed packages of each speech engine
  when it starts. If they are out of date, a notice names the engine
  and asks you to re-run the WheelHouse installer.

### Changed

- The offline Parakeet speech engine now installs the full-precision
  model. It downloads about 2.5 GB in five files instead of the previous
  single archive of 465 MB, and each file is checked against a stored hash
  before it is accepted. An existing smaller model is deleted during the
  upgrade. Recognition accuracy is the reason for the change.
- Wheelhouse now opens the microphone in the Windows Communications
  category instead of the Speech category, so Windows runs its own echo
  canceller against whatever the machine is playing. Recognition quality
  can change for every user, not only for people running a screen
  reader. Tell us if recognition gets worse.
- Wheelhouse now decides for itself whether sound playing on this
  computer pauses listening. It asks Windows whether your microphone
  has an echo canceller and keeps listening when it does. After sound
  stops, listening resumes between 3 and 13 seconds later, where
  before it resumed between 0 and 10 seconds later.
- The Azure speech engine is gone. Google Cloud, Distil-Whisper and
  Parakeet remain.
- Two tray menu items were removed: "Restart Transcription Service" and
  "Google Cloud Credentials". Set the path to your Google key file in the
  settings file instead.
- Wheelhouse no longer uses the separate f.lux program to dim the screen
  below the monitor's own minimum. It dims the screen itself.
- A word that could begin a punctuation name, such as "open" or "new",
  now waits about 0.7 seconds before it is typed when you say it alone.
  That wait is what lets "open bracket" and "question mark" still work
  when you pause in the middle of the phrase.
- The Pattern Manager allows more time to save a custom command, so fewer
  patterns are refused for being slow to save.
- Several help topics were corrected. The help no longer claims Wheelhouse
  speaks AI answers aloud; they appear as notices. It is clearer that
  saying "cut" or "delete all" alone runs the command, while a longer
  phrase containing those words is typed. It also describes what happens
  when Wheelhouse cannot confirm the text box you are dictating into.

### Fixed

- Dictation no longer drops your words when Wheelhouse cannot confirm
  that the focused control accepts text. It pastes the words instead.
  A spoken correction right after a refused insertion no longer deletes
  characters it should not.
- A word you dictate is no longer lost when Wheelhouse cannot read the
  box you are typing into, which can happen in a web browser.
  Wheelhouse now puts the word into the window you were dictating into,
  unless that window changed while the read ran; a read that blocks for
  more than five seconds still loses the word and shows an error notice.
- A sentence that starts with a command word, such as "redo", is now
  typed as a sentence instead of firing the command and losing the rest
  of your words. A sentence that ends with a command word is typed first
  and the command runs afterwards.
- The first letter of a sentence keeps its capital when the first word
  has a capital inside it, such as McDonald, or when the next word is
  also capitalized, such as Bill Smith.
- Dictating into an empty prompt no longer adds a space before your first
  word.
- Punctuation names are more reliable. "question mark" and "exclamation
  mark" at the end of a sentence now insert the symbol, and "full stop"
  is new in this release.
  Saying "okay" right before one of them types "okay?" and not the words.
  A short pause inside a two-word punctuation name, such as "open ...
  bracket", still inserts the symbol.
- Spoken counts work correctly above ten. "backspace fifteen" removes 15
  characters, and a two-word count such as "backspace twenty three"
  removes 23 characters.
- Custom phrases of three words or more in the Pattern Manager now
  replace correctly instead of typing the words you spoke.
- Clicking a numbered badge no longer breaks typing for the rest of the
  session. Before this release, every later dictated word failed until
  you restarted Wheelhouse.
- Badges that belong to a window that has closed no longer stay on screen
  and click the wrong thing. If the numbers change while you are
  speaking, Wheelhouse warns you instead of clicking the wrong item.
- Saying "to", "too" or "for" while the badges are shown now clicks
  the right badge. With the mouse grid open the same words choose
  cell 2 or cell 4.
- "show numbers" waits longer before giving up on a slow application, so
  the badges usually appear instead of an error message. Saying it again,
  or switching windows while the badges are still being drawn, no longer
  costs several seconds. Badges no longer refuse clicks after repeated
  fast requests.
- Numbered badges on wide rows, such as menu items, now appear next to
  the label instead of far away from it.
- Voice-clicking a folder in Windows Explorer's navigation pane no
  longer only expands or collapses it. When a control's own default
  action is Expand or Collapse, Wheelhouse clicks the screen position it
  has checked instead of firing that action, so the folder's contents
  appear. On a Windows installed in a language other than English,
  Wheelhouse may not recognise that default action, and the old
  behaviour can remain.
- Speech engines recover better. If a provider crashes or fails to start,
  the tray display updates instead of showing an engine that is gone, and
  Wheelhouse restarts an engine that was left running after a failed
  switch. Switching engines quickly, or switching back before a switch
  finishes, no longer shows the wrong name or a false failure. An
  unrecognised engine name in the settings file no longer leaves you with
  no working engine.
- The "Transcription service ready" message now appears when the
  microphone actually opens, instead of after a fixed delay. Wheelhouse
  says when a missing audio component or a missing speaker is the reason
  it cannot start.
- The Parakeet engine drops far fewer words when the processor is busy,
  for example while a video plays or a build runs. Word boosting from
  your hint phrases now works; before this release it did nothing at all.
- Push-to-talk is more reliable. Releasing the key restores whether
  listening was on or off beforehand. Push-to-talk keeps working after
  you change the default playback device, mutes the right device when the
  communications-device setting is in use, and restores your speaker
  volume if Wheelhouse closes while the volume is still lowered.
- Manual edits to the settings file while Wheelhouse is running are no
  longer erased the next time Wheelhouse saves a setting. Saving now
  reports a failure after about 20 seconds instead of failing silently.
- Your customised copies of built-in voice commands no longer revert when
  Wheelhouse changes the wording of the built-in command. Duplicating a
  command keeps it as your own separate command.
- Starting a program by voice no longer blocks your next command while
  the program loads, and a failure to start a program by its file name
  now shows a notice instead of failing silently.
- Screen brightness is more reliable. One turn of the brightness wheel
  dims the screen once, not twice, when more than one brightness device
  is present. A refusal from a Sony Bravia television is now recognised
  as a failure instead of being reported as success. When the television
  is offline, the step dims the screen with the software dimmer, and
  repeated steps no longer leave the television's own brightness creeping
  upward once it reconnects.
- Notices that are too long to fit are now shortened with an ellipsis
  instead of not appearing at all. The status box that says "Loading" or
  "Correcting" no longer closes early when an unrelated task finishes.
- Saying "x-ray cancel fix" while an AI rewrite is running now cancels it
  and leaves your original text alone.
- The Debug menu item shows a checkmark when detailed logging was already
  switched on in the settings file at startup.
- If removing your saved AI key during uninstall fails, Wheelhouse shows
  a notice that explains what to do.
- The wake-word model download handles timeouts better and no longer
  keeps a partly downloaded file.

### Known issues

- In Visual Studio Code, the rows of an open menu such as File get no
  numbers, and a click on a row by its name is refused. Visual Studio Code
  reports those rows as off-screen, so Wheelhouse does not find them.

## [1.0.7] - 2026-07-31

### Changed

- The numbered overlay and the mouse grid now use the same words Windows
  Voice Access uses: say "show numbers" and "hide numbers" for the
  numbered overlay, and "show grid" and "hide grid" for the mouse grid.
  The older wordings "apply numbers", "dismiss numbers", "apply grid" and
  "dismiss grid" still work; the new words are the ones the help
  describes, and either wording opens or closes the same thing.

### Fixed

- Installing on Windows 10 no longer fails while unpacking the speech
  model. The installer had used the tar program that ships with Windows,
  and the Windows 10 version of that program cannot open the model's
  compression format. The installer now unpacks the model with
  Wheelhouse's own Python instead, which works the same on Windows 10
  and 11. Interrupted or overlapping installer runs were also hardened:
  a half-finished download resumes on the next run, two installers
  running at the same time no longer damage each other's files, and a
  failure message now includes the actual error instead of a bare exit
  code.
- The Pattern Manager no longer offers an "Open help chat" action. The
  in-app help chat is turned off in this release, so a pattern built on
  that action did nothing when spoken. The action list now shows only
  actions that work, and the "Open online help" example uses the shipped
  "x-ray help" command.

## [1.0.6] - 2026-07-31

### Added

- Wheelhouse can now run an AI model on your own machine, with no account,
  no API key, and no text leaving the computer. Run the command-line
  installer with `-AiMode local` and it downloads a language model and the
  program that runs it, then configures Wheelhouse to start and stop that
  program for you. Setup measures the machine first: a graphics card with
  4 GB or more of video memory runs the model on the card, a machine with
  16 GB or more of system memory runs it on the processor, and anything
  less is told why and left with the AI features off rather than having
  several gigabytes downloaded that would not run. Any graphics card brand
  qualifies. The graphical installer still offers only the cloud option and
  skipping; the local option is command-line only in this release.
- New rewriting commands change the wording of text you have highlighted:
  "x-ray simplify", "x-ray shorten", "x-ray make formal", "x-ray pirate",
  and "x-ray translate to <language>" for any language you name. The style
  is written as an ordinary sentence inside the pattern file, so you can
  add your own rewriting command in the Pattern Manager without any
  programming.
- Setting up the Google Cloud speech engine no longer requires creating a
  Windows environment variable by hand. A new "Google Cloud Credentials"
  item on the tray menu opens a file dialog, checks that the file you pick
  is a valid service-account key, saves it, and restarts the Google engine
  if it is the one running. The menu item appears only when the Google
  engine is installed. Existing setups that use the environment variable
  keep working.
- The floating button can be resized by dragging its edge, and it now
  remembers the size and position you left it at.
- The notification-area icon is now the Wheelhouse icon and no longer
  changes colour with the listening state. Its right-click menu, shared
  with the floating button, gained Help and About entries.
- When setup fails, the installer now tells you what went wrong and what
  to try, shows the full path to the setup log, gives the help address,
  and offers to open the log for you. Previously it said only that details
  were in the log.
- The graphical installer now shows notices on its finish page. If your
  graphics card cannot run the Distil-Whisper speech engine, setup installs
  Parakeet instead and says so, rather than substituting it silently.

### Changed

- The website and the help document have been rewritten as an
  administration guide, and the website's page structure, headings, and
  links were made consistent with it.
- Setup mentions microphone permission only when the permission is
  actually turned off. Both the graphical installer and the command-line
  installer now read the Windows setting and stay quiet when nothing is
  wrong.
- Setup no longer looks for an existing Ollama installation.
- The Pattern Manager explains letter-and-space captures in plain words.

### Fixed

- A command interrupted by a gap in the audio no longer resumes as if it
  were fresh speech, which could join two unrelated phrases into one
  command. Stalls in audio capture and dropped audio now count as gaps for
  this purpose.
- The Google Cloud speech engine is more reliable when it restarts:
  Wheelhouse now waits for the old process to exit, checks that the
  restart succeeded, and no longer leaves the engine stopped when it
  cannot pick a replacement.
- The About box no longer shuts Wheelhouse down when you close it.
- Rewriting a selection that was already a fenced code block keeps its
  fences.
- Microphone diagnostics survive a speech-engine restart instead of being
  lost.
- The local AI server is stopped when Wheelhouse exits, and is replaced if
  it stops answering while still running.
- Four values in the shipped settings template did not match the values
  the program itself uses when a setting is absent. On a fresh install the
  floating button was hidden, too small to click comfortably, and placed
  off the edge of the screen, and logging was set to its most detailed
  level. All four now match: the button starts visible at 50 pixels near
  the top-left corner, and logging starts at the ordinary level.

## [1.0.5] - 2026-07-24

### Added

- The online help and the website now include a separate
  command-and-configuration reference: a full list of every voice command
  and every configuration setting, linked from the main guide. The main
  guide is shorter as a result, and the official Wheelhouse Assistant
  fetches this reference when you ask about a specific command or setting.

### Changed

- The Distil-Whisper speech engine is now labeled "Distil-Whisper Medium"
  everywhere it appears (previously "Distil Medium").

### Fixed

- The graphical installer (`Wheelhouse-Setup.exe`) could stop partway
  through with an "untrusted mount point" error on some Windows machines
  and leave Wheelhouse not installed. It now completes on those machines.
- The working/busy indicator (the small hourglass shown on screen) is
  smaller, and its outline was retuned to match, so it is less obtrusive.

## [1.0.4] - 2026-07-20

### Added

- When the window you are dictating into belongs to a program running
  as administrator, Wheelhouse now shows a notice explaining that
  Windows blocks typing into administrator programs -- and how to fix
  it (restart Wheelhouse with right-click, Run as administrator) --
  instead of silently doing nothing. The terminal dictation editor
  performs the same check before pasting.
- "Wheelhouse help online" now opens the official Wheelhouse Assistant
  by default -- a ChatGPT assistant that always answers from the latest
  released help document.

### Fixed

- The "x-ray" commands are now recognized when the Parakeet speech
  engine splits or fuses the word (for example "x ray" or "xray").
- Help-document corrections: the mouse scroll commands now lead with
  the thumb-wheel behavior, the guidance on mishears and on volume
  commands was corrected, and a five-domain accuracy audit against the
  current code corrected twelve more inaccuracies covering voice
  commands, configuration, installation, speech engines, and plugins.

### Changed

- The product name is now spelled Wheelhouse -- one word, capital W
  only. The installer is now `Wheelhouse-Setup.exe`, new installs go to
  a `Wheelhouse` folder with matching shortcuts and Add/Remove entry,
  and every notice, window title, document, and the website use the new
  spelling. The GitHub repository is now
  `github.com/wheelhouse-project/Wheelhouse` (old links redirect).
  Existing installs keep the old folder spelling until reinstalled;
  behavior is unaffected.
- Installation guidance now leads with the graphical installer
  (`Wheelhouse-Setup.exe`); the PowerShell one-liner remains available
  as an alternative.

## [1.0.3] - 2026-07-18

> Update (2026-07-18): the `WheelHouse-Setup.exe` asset on this release
> was rebuilt and digitally signed (publisher: David Chesley Hite III)
> after publication. All releases from here on are signed.

### Added

- The project website now carries a full documentation page rendered
  directly from the shipped help document, so the site and the in-app
  help can never disagree.
- The `llm/` folder now ships the official Wheelhouse Helper GPT files
  (`gpt-instructions.txt` and `gpt-action-openapi.json`), so you can
  build a ChatGPT GPT that always answers from the latest released help
  document.

### Fixed

- On the Distil-Whisper speech engine, an internal vocabulary-biasing
  feature could garble or drop transcription entirely; it is now
  disabled on that engine.
- A five-domain accuracy audit checked the help document against the
  current code and corrected eight inaccuracies, covering voice
  commands, configuration, installation, speech engines, and the AI
  subsystem.

### Changed

- The help document is now the single source of truth for using and
  installing Wheelhouse: `INSTALL.md` is generated from its
  installation sections (and gained a Speech Engines and Accounts
  overview), and the assistant instructions that previously shipped as
  a separate `llm/assistant-instructions.txt` are now embedded in the
  help document itself -- the `llm/` kit is a single file plus the GPT
  files above.

## [1.0.2] - 2026-07-17

### Added

- Use Wheelhouse help with your own AI assistant: the release now ships an
  `llm/` folder containing the full help document and a ready-to-paste
  assistant instruction file, with step-by-step setup walkthroughs on the
  project site for Custom GPTs, Gemini Gems, Claude Projects, and
  Perplexity Spaces.

### Fixed

- On the default speech engine, saying "comma" or "colon" sometimes typed a
  sound-alike word instead of the punctuation mark. The common mishears are
  now recognized as punctuation.
- The Pattern Manager no longer writes an internal whole-utterance flag
  into saved replacement patterns, which could stop an edited replacement
  from matching during dictation.
- Custom command patterns created in the Pattern Manager's advanced mode
  now carry the whole-utterance-only setting through save and edit
  correctly.
- The installer now logs Start-menu and desktop shortcut creation loudly
  and always writes its setup log, so a failed shortcut is visible instead
  of silent.

### Changed

- The help document was regenerated against the current release, and the
  unused `api_key` line was removed from the shipped configuration template
  (the AI server credential is read only from the `WHEELHOUSE_AI_API_KEY`
  environment variable).

## [1.0.1] - 2026-07-16

### Added

- Graphical installer: `WheelHouse-Setup.exe`, a click-through setup wizard
  that runs the PowerShell installer for you. Built and attached to each
  release automatically. The download was unsigned in this release
  (releases are now digitally signed), so Windows SmartScreen showed a
  warning; see INSTALL.md for the "More info" / "Run anyway" steps.
- Installer AI setup step: choose whether to enable the AI text-correction
  and help features and which server they use. The installer writes the
  server settings into `config.toml` and stores the API key in your user
  environment (never in a file); uninstalling removes the stored key, and
  re-running the installer preserves an existing AI setup by default.
- Installer options for unattended runs (`-SttProvider`, `-AutoStart`,
  `-StartNow`, machine-readable progress output) -- these are what the
  graphical wizard uses to drive the install without console prompts.
- "pattern manager" is now accepted as a spoken trigger for the Pattern
  Manager, alongside "x-ray patterns".

### Fixed

- Speech-engine fallback now resolves to the local Parakeet engine instead
  of cloud Google STT when the configured provider is unavailable -- audio
  no longer leaves the machine unless you explicitly chose a cloud engine.
- The AI API key is read from the environment, not from `config.toml`, so
  a shared or backed-up config file cannot leak it.
- Removed a misleading warning about cloud AI endpoints whose URL does not
  end in `/v1`.
- Dictation now uses the fast caret-position read it was designed to use,
  instead of always taking the slow fallback path.
- Quieter logs: per-keystroke and per-word diagnostic lines no longer
  repeat at INFO level during normal dictation.
- Corrected stale voice-command names and examples in the in-app help.

## [1.0.0] - 2026-07-12

First public release. Wheelhouse was developed privately as its author's
daily driver before this release; 1.0.0 opens the source and makes it
installable by anyone.

### Added

- Voice commands: window switching, key presses, program launch, driven
  by an extensible pattern catalog with a built-in Pattern Manager for
  user-defined commands.
- Streaming dictation into any application, with spoken punctuation,
  context-aware spacing and capitalization, and a text-target check that
  keeps keystrokes out of controls that don't accept text.
- Voice element clicking: `click <name>` finds and clicks controls in the
  focused window; `apply numbers` overlays numbered badges on every
  clickable control for `click <N>`.
- Terminal dictation editor: dictating at a shell prompt opens a small
  editor so text is reviewed before it reaches the terminal.
- Three speech engines: NVIDIA Parakeet via sherpa-onnx (default, local
  CPU, offline), Distil-Whisper (opt-in, local NVIDIA GPU), and Google
  Cloud STT (opt-in, cloud).
- Wake-word support ("computer" by default) via openWakeWord.
- Optional AI features (dictation fix-up, help chat) through any
  OpenAI-compatible server; local Ollama by default; self-disables when
  no server is configured.
- One-command PowerShell installer with hardware preflight, model
  download, and Start-menu integration.
- Privacy defaults: no telemetry, and logs redact dictated content at
  every log level unless `LOG_TRANSCRIPTS = true` is set explicitly.
- Plugins (off by default): Sonos speakers, Sony Bravia TVs, window
  positioning, system volume, idle monitoring, internal display control.
