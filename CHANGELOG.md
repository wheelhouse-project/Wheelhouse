# Changelog

All notable changes to Wheelhouse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
