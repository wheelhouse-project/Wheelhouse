# AGENTS.md

Guidance for AI coding agents (Codex, Copilot, and others) working in this
repository. `CLAUDE.md` carries the shared project rules for all agents —
read it first; this file only adds agent-workflow expectations.

## Mission

- Prioritize reliability, accessibility, and safety over delivery speed.
- Wheelhouse enables full voice control for users who may not use a mouse or
  keyboard; regressions are high impact.

## Working defaults

- Implement tasks end-to-end, but do not trade correctness for speed.
- Ask before architectural changes, destructive actions, dependency strategy
  changes, or changes that reduce test coverage.
- Prefer explicit, deterministic behavior over implicit behavior.
- Every behavior change must include or update tests; every bug fix must
  include a regression test.
- Run targeted tests for touched code, then broader suites for affected
  subsystems: `python scripts/run_tests.py` from the repo root (see
  `CLAUDE.md` for the wrapper's usage; do not run bare `pytest` or pipe its
  output through text filters).
- Do not use direct `pip install` for project dependencies. Use the owning
  service's uv workflow (`uv add`, `uv sync`) from that service's directory.
- Keep changes scoped to the task unless a dependency fix is required; do
  not revert unrelated local changes.

## Critical voice journeys (release gates)

For changes touching these flows, add or update automated tests covering
both success and failure paths. If a gate cannot be validated, report the
gap explicitly with risk level and follow-up:

- wake word detection activates reliably and avoids obvious false triggers
- a valid voice command is recognized and the expected action executes
- an ambiguous or invalid command is rejected safely with clear feedback
- interruption handling stops or reprioritizes actions correctly
- the timeout/no-input path returns the system to a ready state gracefully
- the STT/service failure path provides feedback and recovers hands-free

## Final response format

Keep responses concise and execution-focused. Include: a summary of the
implemented changes, the files touched, the validation commands and their
outcomes, and any open risks or follow-up actions. If any required
verification could not run, report exactly what was not run and why.
