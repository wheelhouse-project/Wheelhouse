# Wheelhouse

Wheelhouse is a free, open-source voice control application for Windows. It supports dictation, keyboard and mouse actions, window management, and clicking on-screen controls by name or number.

It is designed for hands-free computer use, including by people who find using a keyboard or mouse difficult or impossible. Speech recognition runs locally by default, with optional GPU and cloud engines. The default setup needs no account or subscription.

[Installation](https://github.com/wheelhouse-project/Wheelhouse/blob/main/INSTALL.md) · [User guide](https://wheelhouse-project.org/guide.html) · [Command list](https://wheelhouse-project.org/guide.html#voice-commands) · [Settings reference](https://wheelhouse-project.org/reference.html) · [Releases](https://github.com/wheelhouse-project/Wheelhouse/releases)

## Features

- **Dictation:** Insert text into the focused application as you speak, with spoken punctuation and line breaks.
- **Desktop control:** Press keys, switch windows, launch applications, and control the mouse through voice commands.
- **Control selection:** Find and activate controls by name, or display numbered labels and select a control by number.
- **Text insertion checks:** Before typing, Wheelhouse checks that the focused control accepts text, so dictation does not go into the wrong place.
- **Custom commands:** Add and edit voice command patterns through the built-in Pattern Manager.
- **Text correction and rewriting:** Correct spelling and grammar or rewrite selected text using an optional language model, running locally or through a configured cloud service.

## Requirements

- 64-bit Windows 10 or Windows 11. See the installation guide for supported editions.
- 8 GB RAM minimum; 16 GB recommended.
- 10 GB free disk space.
- A microphone.
- An internet connection for installation and model downloads. The default speech engine works offline afterward.

Four or more CPU cores are recommended. A dedicated GPU is not required for the default speech engine.

## Installation

1. Download [Wheelhouse-Setup.exe](https://github.com/wheelhouse-project/Wheelhouse/releases/latest/download/Wheelhouse-Setup.exe) from the latest release.
2. Run the installer and select a speech engine. Parakeet is selected by default.
3. Start Wheelhouse from the Start menu or desktop shortcut.

The installer checks hardware requirements, creates isolated Python environments, downloads the selected speech model, and adds shortcuts. Python does not need to be installed beforehand.

Wheelhouse installs for the current Windows account and needs no administrator rights. The one exception is the Microsoft Visual C++ runtime: if the computer lacks it or has an old copy, Windows asks for permission to install it. If that prompt is declined, setup still finishes, but speech may not start.

The installer is digitally signed by the project's author, David Chesley Hite III. Windows SmartScreen may still warn about a new release until it has seen that file often enough. Select **More info**, check that the publisher reads David Chesley Hite III, then select **Run anyway**. [Security warnings](https://github.com/wheelhouse-project/Wheelhouse/blob/main/INSTALL.md#security-warnings) explains each warning.

For command-line installation, optional AI setup, updates, removal, and troubleshooting, see [INSTALL.md](https://github.com/wheelhouse-project/Wheelhouse/blob/main/INSTALL.md).

## Speech engines

| Engine | Processing | Requirements |
| --- | --- | --- |
| Parakeet (default) | Local CPU | Model download during installation; no account required |
| Distil-Whisper | Local NVIDIA GPU | NVIDIA GPU with at least 4 GB of dedicated video memory; the model downloads the first time the engine starts |
| Google Cloud Speech-to-Text | Google Cloud | Internet connection, a Google Cloud account, and credentials; Google charges for use beyond its free tier |

The local engines keep speech audio and transcripts on the computer. Google Cloud Speech-to-Text sends audio to Google for recognition. Engine setup and switching are covered in the installation guide.

## Privacy

The desktop application sends no telemetry, analytics, or automatic crash reports. Recognized speech is redacted from logs by default; transcript logging can be enabled for troubleshooting.

Optional cloud speech recognition and hosted AI features send their inputs to the configured provider. AI processing is separate from speech recognition: choosing a local speech engine does not make a hosted AI service local.

Desktop control uses microphone access, global input listeners, clipboard access, synthetic keyboard and mouse input, and Windows accessibility interfaces. See [PRIVACY.md](https://github.com/wheelhouse-project/Wheelhouse/blob/main/PRIVACY.md) for data flows, logging settings, and local permissions.

## Documentation and support

| Resource | Contents |
| --- | --- |
| [User guide](https://wheelhouse-project.org/guide.html) | Daily use, dictation, every voice command, and settings |
| [Action, notice, and configuration reference](https://wheelhouse-project.org/reference.html) | Pattern actions, notices, and configuration options |
| [Architecture](https://github.com/wheelhouse-project/Wheelhouse/blob/main/ARCHITECTURE.md) | Processes, communication, and the speech pipeline |
| [Wheelhouse Assistant](https://notebook.google.com/notebook/da51a404-67ec-4804-9ebe-83605df3e9cf/preview) | Answers questions from the latest documentation. It runs on Google's Gemini Notebook and needs a free Google Account. |
| [AI-assisted help](https://github.com/wheelhouse-project/Wheelhouse/blob/main/llm/README.md) | Using the documentation with your own AI chat service |

Report bugs through [GitHub Issues](https://github.com/wheelhouse-project/Wheelhouse/issues). Include the Windows version, speech engine, affected application, and steps to reproduce the problem. Questions can also be sent to [help@wheelhouse-project.org](mailto:help@wheelhouse-project.org).

## Project status and limitations

Wheelhouse is actively developed by a single primary author and has been tested on a limited range of hardware and applications. Compatibility and recognition performance can vary between systems.

Voice control depends in part on the accessibility information applications expose. Some controls may not be discoverable by name or number. Windows also restricts input into applications running with higher privileges; UAC prompts on the secure desktop cannot be controlled by Wheelhouse. [Administrator windows and UAC prompts](https://github.com/wheelhouse-project/Wheelhouse/blob/main/INSTALL.md#administrator-windows-and-uac-prompts) explains how to dictate into programs that run as administrator.

## Contributing

Bug reports, documentation improvements, and code contributions are welcome. Read [CONTRIBUTING.md](https://github.com/wheelhouse-project/Wheelhouse/blob/main/CONTRIBUTING.md) for development setup, testing, and the pull request workflow. Wheelhouse uses Python and uv, with separate environments for its services.

Changes must preserve hands-free operation and include tests for changed behavior. Report security vulnerabilities using the process in [SECURITY.md](https://github.com/wheelhouse-project/Wheelhouse/blob/main/SECURITY.md).

## License and acknowledgements

Wheelhouse is licensed under the [Apache License 2.0](https://github.com/wheelhouse-project/Wheelhouse/blob/main/LICENSE).

The default speech engine uses NVIDIA Parakeet through sherpa-onnx. Wake-word detection uses openWakeWord, and notification sounds include audio from Pixabay. Third-party licenses and attribution are documented in [NOTICE](https://github.com/wheelhouse-project/Wheelhouse/blob/main/NOTICE) and [PROVENANCE.toml](https://github.com/wheelhouse-project/Wheelhouse/blob/main/PROVENANCE.toml).
