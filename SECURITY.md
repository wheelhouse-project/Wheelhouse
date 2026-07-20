# Security Policy

Wheelhouse is a voice-control application whose users may depend on it
for all computer input, and whose dictation can include passwords and
medical text. Security and privacy reports are treated as high priority.

## Reporting a vulnerability

**Do not open a public issue for a suspected vulnerability.**

Report it privately through GitHub's vulnerability reporting on this
repository: **Security → Report a vulnerability** (or
`https://github.com/wheelhouse-project/Wheelhouse/security/advisories/new`),
or by email to <security@wheelhouse-project.org>. You will
get a response from the maintainer, normally within a week.

Include what you can:

- What an attacker gains (read dictated text? inject input? escalate?).
- Steps to reproduce, affected version (`VERSION` file or release tag),
  and your Windows version.
- Whether the issue requires local access, a malicious application on the
  same machine, or network position.

If the report is valid, a fix is developed privately, released, and the
advisory published with credit to the reporter (unless you prefer
otherwise). There is no bug bounty; this is a volunteer project.

## Scope guidance

Reports especially welcome:

- Dictated or clipboard content leaking anywhere it shouldn't (logs,
  files, network) — the redaction default (PRIVACY.md) failing is a
  vulnerability, not a bug.
- Wheelhouse's IPC channels (WebSocket, shared memory, queues) being
  usable by another local process to inject synthetic input or read
  transcripts.
- The installer or model download executing or trusting something it
  shouldn't (unverified downloads, path hijacks, privilege mistakes).
- Voice commands triggering actions that bypass the safety gates
  described in ARCHITECTURE.md.

Known boundaries, documented rather than reportable:

- Wheelhouse cannot type into or click elevated windows or UAC prompts
  (Windows blocks synthetic input across integrity levels). This is a
  platform boundary, not a Wheelhouse control.
- Anyone who can speak within range of the microphone can issue voice
  commands. There is no speaker authentication in v1; do not point
  Wheelhouse at a machine whose compromise-by-voice would be a security
  boundary violation for you.
- Release installers are digitally signed (publisher: David Chesley
  Hite III). SmartScreen may still warn shortly after each new release
  until the file accrues reputation; documented in INSTALL.md.

## Supported versions

Only the latest release receives security fixes.

## Dependency vulnerabilities

Dependency trees are audited before each release. If you find an exploitable
path through one of the pinned dependencies (see each service's
`uv.lock`), report it here as well as upstream.
