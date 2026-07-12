# Changelog

All notable changes to WheelHouse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-12

First public release. WheelHouse was developed privately as its author's
daily driver before this release; 1.0.0 opens the source and makes it
installable by anyone.

### Added

- Voice commands: window switching, key presses, program launch, volume
  and brightness control, driven by an extensible pattern catalog with a
  built-in Pattern Manager for user-defined commands.
- Streaming dictation into any application, with spoken punctuation,
  context-aware spacing and capitalization, and a text-target check that
  keeps keystrokes out of controls that don't accept text.
- Voice element clicking: `click <name>` finds and clicks controls in the
  focused window; `show numbers` overlays numbered badges on every
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
