# Privacy

WheelHouse is a voice-control application built for people who depend on hands-free computer use. That makes privacy a safety property, not a feature: the things you dictate can include passwords, medical information, and legal or financial text. This document states plainly what stays on your machine, what can leave it, what the application can observe and modify locally, and how the separate **WheelHouse Help** custom GPT handles data.

## No telemetry

The WheelHouse desktop application sends no telemetry, analytics, crash reports, or usage data to the project or to any third party. There is no phone-home code. The only network connections made by the application are the ones you configure, listed below.

## What leaves your machine, per speech provider

| Provider                            | Audio                                         | Transcribed text          | Notes                                                                        |
| ----------------------------------- | --------------------------------------------- | ------------------------- | ---------------------------------------------------------------------------- |
| Parakeet (default)                  | Never leaves your machine                     | Never leaves your machine | Fully offline after the one-time model download at install                   |
| Distil-Whisper (opt-in, NVIDIA GPU) | Never leaves your machine                     | Never leaves your machine | Downloads its model from Hugging Face once on first run                      |
| Google Cloud STT (opt-in)           | Streams to Google's servers while you dictate | Returned by Google        | Governed by Google Cloud's terms; requires your own Google Cloud credentials |

## AI features (optional)

The dictation fix-up and help-chat features send the text they operate on to the OpenAI-compatible server you configure (`[ai.server]` in `config.toml`). With the default configuration that is a local Ollama server, the text stays on your machine. If you point it at a hosted endpoint, that text leaves your machine under that provider's terms. If no server is configured or reachable, the AI features disable themselves and nothing is sent anywhere.

## Logs

* Log files live in the installation directory (`wheelhouse.log`, rotated, plus a small watchdog log). The STT providers log to the same console session and forward status lines to the WheelHouse GUI log pane.
* **By default, logs never contain what you dictate.** Log lines that would carry recognized speech, clipboard content, or vocabulary hints record a length-only placeholder instead (for example `<redacted: 22 chars, 5 words>`). This holds at every log level, so turning on debug logging does not expose content.
* One documented switch re-enables full transcript logging for troubleshooting recognition problems: set `LOG_TRANSCRIPTS = true` in `config.toml` and restart. While it is on, everything you dictate is written to the log files. Turn it back off when you are done, and consider deleting the log files afterward.
* The Google provider keeps a local usage-metrics file (`stt_usage.csv`: timestamps, utterance duration, word counts) for cost tracking. Its text column follows the same rule: placeholder by default, real text only while `LOG_TRANSCRIPTS = true`.

## What WheelHouse can observe and modify on your machine

Hands-free control requires powers that a normal application does not have. In plain language, WheelHouse:

* **Listens to your microphone** while running. Audio is processed by the speech provider you chose (see the table above for where it goes).
* **Watches keyboard and mouse events globally.** The input process runs system-wide listeners; they power the keyboard filter during dictation and voice-controlled mouse movement. WheelHouse does not record keystrokes; the listener state is used in the moment and discarded.
* **Reads and writes the clipboard.** Some text-insertion methods paste through the clipboard (saving and restoring what was there), and the clipboard-capture voice command reads it on request. Clipboard content follows the same log-redaction rule as dictated text.
* **Injects synthetic keyboard and mouse input.** This is the mechanism that types what you dictate and clicks what you name.
* **Reads the UI structure of the focused window** (via Windows UI Automation) to decide whether a control accepts dictated text and to find controls for voice clicking. These reads stay in memory.

Boundaries and known limits:

* **Elevated windows and UAC prompts are out of reach.** Windows blocks synthetic input into processes running with higher privileges, and UAC consent prompts appear on a secure desktop that synthetic input cannot reach. WheelHouse cannot type into or click these; see `INSTALL.md` for workarounds.
* Everything WheelHouse learns about your machine (window titles, control names, hardware details) is used locally and is not transmitted.

## Files WheelHouse writes about you

All of these stay on your machine and are excluded from any repository:

* `config.toml` — your settings.
* `data/user_patterns.toml` — voice command patterns you create.
* `services/stt_providers/shared/hints.txt` — vocabulary words you add for better recognition.
* `data/soft_allow_*.toml` — the list of controls you approved or declined for dictation.
* `%APPDATA%\WheelHouse` and `%LOCALAPPDATA%\WheelHouse` — runtime state, downloaded models, and the per-machine model-path override file.

Uninstalling removes all of the above: the application, both AppData folders, and every file listed. If you tell the uninstaller to keep your personal data, it instead saves your settings, voice patterns, vocabulary words, and the lists of controls you approved or declined for dictation in a `preserved-user-data` folder under `%LOCALAPPDATA%\WheelHouse`, and it keeps the downloaded speech model there too; everything else is still removed. The uninstall section of `INSTALL.md` describes both options.

# WheelHouse Help GPT

The WheelHouse project also publishes the **WheelHouse Help** custom GPT for ChatGPT. This GPT is separate from the WheelHouse desktop application.

## How it works

Before answering WheelHouse-specific questions, the GPT uses an OpenAI Action to retrieve the latest public WheelHouse documentation from the project's GitHub repository.

The Action performs a read-only HTTPS request to download a public Markdown documentation file hosted on GitHub. It does not modify any data, access private repositories, or require authentication.

## What information is sent

The Action retrieves a fixed public documentation file only.

It does **not** transmit your ChatGPT prompts, conversation history, account information, or personal information to the WheelHouse project as part of the request.

The request is made to GitHub's content delivery service (`raw.githubusercontent.com`) solely to retrieve the current documentation. GitHub may receive standard HTTP request metadata (such as IP address, request time, and user agent) as part of serving the file. That processing is governed by GitHub's own privacy policy.

## Data collection

The WheelHouse project does not collect, receive, or store your ChatGPT conversations through the documentation Action.

The Action:

* is read-only;
* requires no authentication;
* creates no user accounts;
* stores no user-specific data; and
* performs no analytics or telemetry on GPT users.

## OpenAI

Your conversations with the WheelHouse Help GPT are processed by OpenAI as part of providing the ChatGPT service and are subject to OpenAI's own Terms of Use and Privacy Policy.

Unless you separately choose to share information with the WheelHouse project (for example, by opening a GitHub issue or discussion), the project does not receive your ChatGPT conversation history.

## Questions

If anything here is unclear or appears incorrect, please open an issue or start a discussion on the WheelHouse GitHub repository.

Security and privacy reports are also welcome through the process described in `SECURITY.md`.
