# Changelog

All notable changes to WheelHouse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2026-07-17

### Added

- Use WheelHouse help with your own AI assistant: the release now ships an
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
  release automatically. The download is unsigned in this release, so
  Windows SmartScreen shows a warning; see INSTALL.md for the
  "More info" / "Run anyway" steps.
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

First public release. WheelHouse was developed privately as its author's
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
