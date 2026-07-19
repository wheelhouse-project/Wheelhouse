# Wheelhouse

> **Voice-controlled desktop automation system** - A multi-process Python application for anyone who wants to control their Windows PC by voice — for hands-free convenience, and equally as serious assistive technology. Built for the open-source community across diverse hardware. **Reliability is non-negotiable**: this project enables full hands-free computer use for people who may not be able to fall back to a mouse or keyboard, so regressions are high impact.

## Architecture

Read `ARCHITECTURE.md` for the process model (launcher, logic, input, GUI, STT
providers), the IPC channels between them, and the speech pipeline flow. The
short version: separate OS processes talk over WebSocket (STT transcripts),
SharedMemory (UI commands), and queues (GUI state); do not share Python
objects across those boundaries.

## Repository layout

Each service has its own uv-managed virtual environment:

| Path | What it is |
|------|------------|
| `services/wheelhouse/` | The main application (logic, input, GUI processes) |
| `services/syscheck/` | Hardware detection used by the installer |
| `services/stt_providers/shared/` | Code shared by the STT providers (consumed as an editable path dependency; do not `uv sync` it standalone) |
| `services/stt_providers/sherpa_offline_parakeet_stt_server/` | Default offline STT provider (Parakeet TDT, CPU) |
| `services/stt_providers/distil_medium_en/` | CUDA STT provider (Distil-Whisper) |
| `services/stt_providers/google_stt_server/` | Google Cloud STT provider |

Run `uv` and `python` commands from the service directory that owns the code.

## Tests

Run tests through the wrapper from the repo root — not bare `pytest`, and
never piped through `tail`/`grep` (the wrapper parses JUnit XML so results
survive pipe truncation):

```bash
python scripts/run_tests.py                        # full main-app suite
python scripts/run_tests.py -k <pattern>           # targeted tests
python scripts/run_tests.py --service <name>       # another service
```

Development is test-driven: write the failing test first, confirm it fails,
implement, confirm it passes, then run the affected suites. Every behavior
change updates tests; every bug fix includes a regression test.

## Configuration

| File | Purpose |
|------|---------|
| `services/wheelhouse/config.toml.example` | Template for the runtime config (`config.toml` is per-machine, untracked) |
| `services/wheelhouse/speech/config/patterns.toml` | Shipped voice-command patterns (read-only; general-purpose commands only) |
| `data/user_patterns.toml` | Personal voice patterns (per-machine, untracked; written by the Pattern Manager) |

## Windows specifics

- Console output must survive CP1252 terminals: no emojis in code, logs, or
  test output. Use `[!]`, `[x]`, `[+]` markers instead.
- For Windows API work (ctypes, SendInput, clipboard, SharedMemory IPC),
  always use 64-bit compatible types; access violations often stem from
  32/64-bit type mismatches.
- Treat Windows behavior as first-class and verify Windows-specific paths.

## Safety expectations for changes

- Preserve hands-free operation paths; core flows must not require a
  keyboard or mouse fallback.
- Favor fail-safe behavior: uncertain recognition must not trigger
  dangerous actions.
- Consider partial failure: what happens when one process crashes, hangs,
  or restarts mid-utterance?
- Evaluate hot-path changes for end-to-end voice latency (microphone to UI
  action); avoid blocking I/O and extra inter-process round trips there.

See `CONTRIBUTING.md` for the contribution workflow.
