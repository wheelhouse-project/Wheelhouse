# Privacy

WheelHouse is a voice-control application built for people who depend on
hands-free computer use. That makes privacy a safety property, not a
feature: the things you dictate can include passwords, medical
information, and legal or financial text. This document states plainly
what stays on your machine, what can leave it, and what the application
can observe and modify locally.

## No telemetry

WheelHouse sends no telemetry, analytics, crash reports, or usage data to
the project or to any third party. There is no phone-home code. The only
network connections it makes are the ones you configure, listed below.

## What leaves your machine, per speech provider

| Provider | Audio | Transcribed text | Notes |
|----------|-------|------------------|-------|
| Parakeet (default) | Never leaves your machine | Never leaves your machine | Fully offline after the one-time model download at install |
| Distil-Whisper (opt-in, NVIDIA GPU) | Never leaves your machine | Never leaves your machine | Downloads its model from Hugging Face once on first run |
| Google Cloud STT (opt-in) | Streams to Google's servers while you dictate | Returned by Google | Governed by Google Cloud's terms; requires your own Google Cloud credentials |

## AI features (optional)

The dictation fix-up and help-chat features send the text they operate on
to the OpenAI-compatible server you configure (`[ai.server]` in
`config.toml`). With the default configuration that is a local Ollama
server, so the text stays on your machine. If you point it at a hosted
endpoint, that text leaves your machine under that provider's terms. If
no server is configured or reachable, the AI features disable themselves
and nothing is sent anywhere.

## Logs

- Log files live in the installation directory (`wheelhouse.log`, rotated,
  plus a small watchdog log). The STT providers log to the same console
  session and forward status lines to the WheelHouse GUI log pane.
- **By default, logs never contain what you dictate.** Log lines that
  would carry recognized speech, clipboard content, or vocabulary hints
  record a length-only placeholder instead (for example
  `<redacted: 22 chars, 5 words>`). This holds at every log level, so
  turning on debug logging does not expose content.
- One documented switch re-enables full transcript logging for
  troubleshooting recognition problems: set `LOG_TRANSCRIPTS = true` in
  `config.toml` and restart. While it is on, everything you dictate is
  written to the log files — turn it back off when you are done, and
  consider deleting the log files afterward.
- The Google provider keeps a local usage-metrics file
  (`stt_usage.csv`: timestamps, utterance duration, word counts) for
  cost tracking. Its text column follows the same rule: placeholder by
  default, real text only while `LOG_TRANSCRIPTS = true`.

## What WheelHouse can observe and modify on your machine

Hands-free control requires powers that a normal application does not
have. In plain language, WheelHouse:

- **Listens to your microphone** while running. Audio is processed by the
  speech provider you chose (see the table above for where it goes).
- **Watches keyboard and mouse events globally.** The input process runs
  system-wide listeners; they power the keyboard filter during dictation
  and voice-controlled mouse movement. WheelHouse does not record
  keystrokes; the listener state is used in the moment and discarded.
- **Reads and writes the clipboard.** Some text-insertion methods paste
  through the clipboard (saving and restoring what was there), and the
  clipboard-capture voice command reads it on request. Clipboard content
  follows the same log-redaction rule as dictated text.
- **Injects synthetic keyboard and mouse input.** This is the mechanism
  that types what you dictate and clicks what you name.
- **Reads the UI structure of the focused window** (via Windows UI
  Automation) to decide whether a control accepts dictated text and to
  find controls for voice clicking. These reads stay in memory.

Boundaries and known limits:

- **Elevated windows and UAC prompts are out of reach.** Windows blocks
  synthetic input into processes running with higher privileges, and UAC
  consent prompts appear on a secure desktop nothing synthetic can reach.
  WheelHouse cannot type into or click these; see INSTALL.md for what
  this looks like and the workarounds.
- Everything WheelHouse learns about your machine (window titles, control
  names, hardware details) is used locally and is not transmitted.

## Files WheelHouse writes about you

All of these stay on your machine and are excluded from any repository:

- `config.toml` — your settings.
- `data/user_patterns.toml` — voice command patterns you create.
- `services/stt_providers/shared/hints.txt` — vocabulary words you add
  for better recognition.
- `data/soft_allow_*.toml` — the list of controls you approved or
  declined for dictation.
- `%APPDATA%\WheelHouse` and `%LOCALAPPDATA%\WheelHouse` — runtime state,
  downloaded models, and the per-machine model-path override file.

Uninstalling removes the installation directory; the two AppData folders
are listed in INSTALL.md's uninstall section so you can remove them too.

## Questions

Open a GitHub issue if anything here is unclear or looks wrong. Privacy
reports are welcome through the process in SECURITY.md.
