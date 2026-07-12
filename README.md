# WheelHouse

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)

**Hands-free control of your Windows PC by voice.**

WheelHouse is a voice-control system built for people who need it: if using
a keyboard and mouse is painful, difficult, or impossible, WheelHouse aims
to give you the whole computer by voice — commands, dictation into any
application, and clicking things by name. It runs entirely on your machine
by default: no cloud account, no subscription, no telemetry.

## What it does

- **Dictate anywhere.** Speak into the focused application and watch the
  words stream in as you talk — the first word typically lands in under two
  seconds and the rest flow continuously, instead of appearing all at once
  after you stop. Spoken punctuation ("comma", "new line") becomes symbols.
- **Voice commands.** Switch windows, press keys, launch programs — driven
  by a pattern catalog you can extend with your own commands through the
  built-in Pattern Manager.
- **Click by voice.** Say `click cancel` or `click the submit button` and
  WheelHouse finds the control in the focused window and clicks it. When
  names are ambiguous or unlabeled, say `apply numbers` to badge every
  clickable control with a number and `click 5` to pick one.
- **Offline by default.** The default speech engine (NVIDIA Parakeet,
  running locally on your CPU) never sends audio or text anywhere.
- **Careful about where text goes.** Before typing a word, WheelHouse
  checks that the focused control actually accepts text, so dictation
  does not spray keystrokes into the wrong place.

## Install

One command, in any PowerShell window:

```powershell
irm https://github.com/wheelhouse-project/WheelHouse/releases/latest/download/install-wheelhouse.ps1 | iex
```

The installer checks your hardware, installs its own Python environment
(nothing global), downloads the offline speech model, and puts WheelHouse
in your Start menu. Details, prerequisites for the optional speech engines,
troubleshooting, and uninstall instructions are in [INSTALL.md](./INSTALL.md).

**Requirements:** Windows 10 or 11 (64-bit), a microphone, and a few GB of
disk space for the speech model. See INSTALL.md for the exact hardware
guidance.

**A note on security warnings:** WheelHouse is currently unsigned — there
is no code-signing certificate yet — so Windows SmartScreen or your
antivirus may warn you the first time you run it. The warnings are about
the missing signature, not about what the software does; the entire source
code is in this repository, and INSTALL.md explains each warning and how to
proceed. If that trade-off is not acceptable to you, you can read the code
and install from source instead (see CONTRIBUTING.md).

## Speech engines

| Engine | Where speech is processed | When to choose it |
|--------|---------------------------|-------------------|
| **Parakeet** (default) | On your machine, CPU | No account, no cloud, works offline. The default for everyone. |
| **Distil-Whisper** (opt-in) | On your machine, NVIDIA GPU | You have a CUDA-capable GPU and want lower latency. |
| **Google Cloud STT** (opt-in) | Google's servers | You have a Google Cloud account and prefer its recognition quality; audio streams to Google while you dictate. |

## Privacy

Privacy is a safety property for a voice-control system — dictation can
include passwords and medical text. The short version:

- **No telemetry.** Nothing is reported to the project or anyone else.
- **Offline by default.** With the default engine, audio and transcripts
  never leave your machine. Only the engines and AI features you opt into
  make network connections, and [PRIVACY.md](./PRIVACY.md) states exactly
  what each one sends.
- **Logs don't contain what you dictate.** By default, log lines record
  placeholders instead of recognized speech, at every log level.
- **Broad local powers, disclosed plainly.** Hands-free control requires
  the microphone, global input listeners, clipboard access, synthetic
  input, and reading the UI of the focused window. PRIVACY.md lists each
  power, why it is needed, and its limits.

## Documentation

| Document | What's in it |
|----------|--------------|
| [INSTALL.md](./INSTALL.md) | Installation in detail, optional engines, troubleshooting, uninstall |
| [User help](./services/wheelhouse/knowledge/wheelhouse_help.md) | Every voice command and feature, for daily use |
| [PRIVACY.md](./PRIVACY.md) | Data flow, logging, and the capability disclosure |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Process model, IPC, and the speech pipeline |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Development setup, tests, and the contribution workflow |
| [SECURITY.md](./SECURITY.md) | Reporting vulnerabilities |

## Project status

WheelHouse is a young open-source project with a single primary author. It
has been the author's daily driver for years and reliability is the
project's first value — but it has so far been validated on a small set of
machines, so expect rough edges on hardware and applications it has not
met yet. Bug reports are genuinely welcome, especially from users who
depend on hands-free input: if WheelHouse fails you, that is exactly the
report the project needs.

## Acknowledgements

- The default speech model is [NVIDIA Parakeet TDT 0.6B](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
  (CC-BY-4.0, NVIDIA NeMo), served through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).
- Wake-word detection uses [openWakeWord](https://github.com/dscripka/openWakeWord)
  community models.
- Notification sounds are from [Pixabay](https://pixabay.com/sound-effects/).
- Full third-party attribution lives in [NOTICE](./NOTICE) and
  [PROVENANCE.toml](./PROVENANCE.toml).

## License

Apache License 2.0 — see [LICENSE](./LICENSE).
