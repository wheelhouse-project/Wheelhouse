# Contributing to Wheelhouse

Thank you for considering a contribution. Wheelhouse enables full
hands-free computer use for people who may not be able to fall back to a
mouse or keyboard, so the bar for reliability is deliberately high — this
document explains the workflow that keeps it that way.

## Ways to contribute

- **Bug reports** are the most valuable contribution, especially from
  users who depend on hands-free input or run hardware the project has
  not seen. Use the bug-report issue template and include your log file
  (logs contain no dictated content by default — see PRIVACY.md).
- **Feature discussion** belongs in GitHub Discussions before code: for a
  voice-controlled system, command grammar and failure behavior need
  agreement before implementation.
- **Pull requests** for fixes and features are welcome; read the workflow
  below first.

## Development setup

Wheelhouse is a uv-managed monorepo of independent Python services, each
with its own virtual environment. You need:

- Windows 10 or 11 (64-bit) — this is a Windows application; most of the
  input/UI code has no meaning elsewhere.
- [uv](https://docs.astral.sh/uv/) (it provides Python 3.12 itself).
- A microphone, for end-to-end testing.

```powershell
git clone https://github.com/wheelhouse-project/Wheelhouse
cd Wheelhouse/services/wheelhouse
uv sync
copy config.toml.example config.toml
uv run python launcher.py
```

Each service (`services/wheelhouse`, `services/syscheck`, the three
providers under `services/stt_providers/`) syncs and runs from its own
directory. **Do not `uv sync` `services/stt_providers/shared` standalone**
— it is consumed as an editable path dependency inside each provider's
venv.

The default STT provider needs the Parakeet model on disk; the easiest
way to get it is to run the regular installer once (see INSTALL.md) — the
development checkout will find the model through the same per-machine
override file the installer writes.

## Tests

Run tests through the wrapper from the repo root — not bare `pytest`, and
never piped through `tail`/`grep` (the wrapper parses JUnit XML so results
survive pipe truncation):

```powershell
python scripts/run_tests.py                        # full main-app suite
python scripts/run_tests.py -k <pattern>           # targeted tests
python scripts/run_tests.py --service <name>       # another service
```

Development is test-driven: write the failing test first, confirm it
fails, implement, confirm it passes, then run the affected suites. Every
behavior change updates tests; every bug fix includes a regression test.
Pull requests without tests for the changed behavior will be asked to add
them.

## What reviewers look for

These come from the project's safety expectations (see CLAUDE.md /
AGENTS.md, which the project's AI tooling also follows):

- **Hands-free paths stay hands-free.** Core flows must not require a
  keyboard or mouse fallback.
- **Fail safe.** Uncertain recognition must not trigger dangerous
  actions.
- **Partial failure is normal.** Launcher, logic, input, GUI, and STT run
  as separate processes; consider what happens when one crashes, hangs,
  or restarts mid-utterance.
- **Latency is a feature.** Evaluate hot-path changes for end-to-end
  voice latency (microphone to UI action); avoid blocking I/O and extra
  inter-process round trips there.
- **Process boundaries are real.** Do not share Python objects across
  processes; use the established IPC channels (see ARCHITECTURE.md).

Windows specifics:

- No emojis in code, logs, or test output — console output must survive
  CP1252 terminals. Use `[!]`, `[x]`, `[+]` markers instead.
- For Windows API work (ctypes, SendInput, clipboard, SharedMemory IPC),
  use 64-bit compatible types; access violations often stem from
  32/64-bit type mismatches.

## Pull request workflow

1. Fork, branch from `main`, keep the change scoped to one logical thing.
2. Follow the test-driven flow above; run the affected service suites.
3. Sign off your commits (Developer Certificate of Origin):
   `git commit -s`. The sign-off certifies you have the right to submit
   the change under the project license
   ([developercertificate.org](https://developercertificate.org/)).
4. Use `<type>: <description>` commit subjects (`feat`, `fix`,
   `refactor`, `chore`, `docs`, `test`).
5. Open the PR against `main` and fill in the template. CI must pass.

## The evaluation harness (optional, advanced)

`services/stt_providers/evaluation/` compares STT providers and is not
part of the shipped application. Its whisper.cpp path uses a custom
GPU (Vulkan) build of `pywhispercpp` — the PyPI build is CPU-only. If you
work on the harness, the build recipe is in
`docs/design/stt-and-ai.md` (section 4). Everything shipped resolves from
PyPI; you never need this to contribute to the application itself.

## Questions

Open a GitHub Discussion. For suspected security problems, follow
SECURITY.md instead of filing a public issue.
