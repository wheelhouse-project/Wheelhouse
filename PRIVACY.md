# Privacy

Wheelhouse is a voice-control application built for people who depend on hands-free computer use. That makes privacy a safety property, not a feature: the things you dictate can include passwords, medical information, and legal or financial text. This document states plainly what stays on your machine, what can leave it, what the application can observe and modify locally, and how the separate **Wheelhouse Assistant** handles data.

## No telemetry

The Wheelhouse desktop application sends no telemetry, analytics, crash reports, or usage data to the project or to any third party. There is no phone-home code. The only network connections made by the application are the ones you configure, listed below.

## What leaves your machine, per speech provider

| Provider                            | Audio                                         | Transcribed text          | Notes                                                                        |
| ----------------------------------- | --------------------------------------------- | ------------------------- | ---------------------------------------------------------------------------- |
| Parakeet (default)                  | Never leaves your machine                     | Never leaves your machine | Fully offline after the one-time model download at install                   |
| Distil-Whisper (opt-in, NVIDIA GPU) | Never leaves your machine                     | Never leaves your machine | Downloads its model from Hugging Face once on first run                      |
| Google Cloud STT (opt-in)           | Streams to Google's servers while you dictate | Returned by Google        | Governed by Google Cloud's terms; requires your own Google Cloud credentials |

## AI features (optional)

The dictation fix-up and help-chat features send the text they operate on to the OpenAI-compatible server you configure (`[ai.server]` in `config.toml`). With the default configuration that is a local Ollama server, the text stays on your machine. If you point it at a hosted endpoint, that text leaves your machine under that provider's terms. If no server is configured or reachable, the AI features disable themselves and nothing is sent anywhere.

## Logs

* Log files live in the installation directory (`wheelhouse.log`, rotated, plus a small watchdog log). The STT providers log to the same console session and forward status lines to the Wheelhouse GUI log pane.
* **By default, logs never contain what you dictate.** Log lines that would carry recognized speech, clipboard content, or vocabulary hints record a length-only placeholder instead (for example `<redacted: 22 chars, 5 words>`). This holds at every log level, so turning on debug logging does not expose content.
* One documented switch re-enables full transcript logging for troubleshooting recognition problems: set `LOG_TRANSCRIPTS = true` in `config.toml` and restart. While it is on, everything you dictate is written to the log files. Turn it back off when you are done, and consider deleting the log files afterward.
* The Google provider keeps a local usage-metrics file (`stt_usage.csv`: timestamps, utterance duration, word counts) for cost tracking. Its text column follows the same rule: placeholder by default, real text only while `LOG_TRANSCRIPTS = true`.

## What Wheelhouse can observe and modify on your machine

Hands-free control requires powers that a normal application does not have. In plain language, Wheelhouse:

* **Listens to your microphone** while running. Audio is processed by the speech provider you chose (see the table above for where it goes).
* **Watches keyboard and mouse events globally.** The input process runs system-wide listeners; they power the keyboard filter during dictation and voice-controlled mouse movement. Wheelhouse does not record keystrokes; the listener state is used in the moment and discarded.
* **Reads and writes the clipboard.** Some text-insertion methods paste through the clipboard (saving and restoring what was there), and the clipboard-capture voice command reads it on request. Clipboard content follows the same log-redaction rule as dictated text.
* **Injects synthetic keyboard and mouse input.** This is the mechanism that types what you dictate and clicks what you name.
* **Reads the UI structure of the focused window** (via Windows UI Automation) to decide whether a control accepts dictated text and to find controls for voice clicking. These reads stay in memory.

Boundaries and known limits:

* **Elevated windows and UAC prompts are out of reach.** Windows blocks synthetic input into processes running with higher privileges, and UAC consent prompts appear on a secure desktop that synthetic input cannot reach. Wheelhouse cannot type into or click these; see `INSTALL.md` for workarounds.
* Everything Wheelhouse learns about your machine (window titles, control names, hardware details) is used locally and is not transmitted.

## Files Wheelhouse writes about you

All of these stay on your machine and are excluded from any repository:

* `config.toml` — your settings.
* `data/user_patterns.toml` — voice command patterns you create.
* `services/stt_providers/shared/hints.txt` — vocabulary words you add for better recognition.
* `data/soft_allow_*.toml` — the list of controls you approved or declined for dictation.
* `%APPDATA%\WheelHouse` and `%LOCALAPPDATA%\WheelHouse` — runtime state, downloaded models, and the per-machine model-path override file.

Uninstalling removes all of the above: the application, both AppData folders, and every file listed. If you tell the uninstaller to keep your personal data, it instead saves your settings, voice patterns, vocabulary words, and the lists of controls you approved or declined for dictation in a `preserved-user-data` folder under `%LOCALAPPDATA%\WheelHouse`, and it keeps the downloaded speech model there too; everything else is still removed. The uninstall section of `INSTALL.md` describes both options.

# Wheelhouse Assistant

The Wheelhouse project also publishes the **Wheelhouse Assistant**, a Google Gemini Gem, at <https://gemini.google.com/gem/1z3my7h0wNiR2msZW8_NAEzxboZOTjN2A>. The assistant is separate from the Wheelhouse desktop application. You use it in your web browser, and Gemini asks you to sign in first: a Google account, an Apple account, or an email address works, and a free account is enough. Nothing you say to the assistant touches the desktop application, and the desktop application sends nothing to it.

## How it works

The assistant holds the current Wheelhouse documentation as stored files in its own Knowledge section: the help document, the installation guide, and the command and configuration reference. It reads those stored files and answers from them.

The assistant retrieves nothing while it answers. It makes no request to the Wheelhouse project, to GitHub, or to any other site, so using it tells the project nothing at all.

The project keeps those stored files current by rewriting them when it publishes a release. That rewrite runs on a project maintainer's machine. It carries documentation to Google and carries nothing back.

## What the project receives

The Wheelhouse project does not collect, receive, or store your conversations with the assistant.

The project:

* receives no prompt you type and no answer you get;
* creates no user account for you;
* stores no data about you; and
* performs no analytics and no telemetry on assistant users.

## Google

Your conversations with the Wheelhouse Assistant are processed by Google as part of providing the Gemini service. They are subject to Google's own terms of service and privacy policy, and to whatever data settings you have chosen in the account you signed in with. Read those before you dictate anything sensitive to the assistant.

Unless you separately choose to share information with the Wheelhouse project (for example, by opening a GitHub issue or discussion), the project does not receive your conversation history.

## Questions

If anything here is unclear or appears incorrect, please open an issue or start a discussion on the Wheelhouse GitHub repository.

Security and privacy reports are also welcome through the process described in `SECURITY.md`.
