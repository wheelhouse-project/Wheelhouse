"""Transcript redaction for log lines (wh-transcript-log-defaults).

Dictated text and clipboard content are as sensitive as passwords for the
accessibility audience, and rotated logs used to accumulate everything the
user said. Release default: log lines carry a length-only placeholder
instead of content. The single documented switch is LOG_TRANSCRIPTS in
config.toml; launcher.py reads it once and exports
WHEELHOUSE_LOG_TRANSCRIPTS to every child process (the STT providers
inherit it through RemoteSTTLauncher's environment copy).

Only the exact value "1" enables full logging, so any process started
outside the launcher defaults to the privacy-safe state.
"""
from __future__ import annotations

import os

ENV_VAR = "WHEELHOUSE_LOG_TRANSCRIPTS"


def transcript_logging_enabled() -> bool:
    """True only when the launcher exported WHEELHOUSE_LOG_TRANSCRIPTS=1."""
    return os.environ.get(ENV_VAR) == "1"


def redact_transcript(text) -> str:
    """Return ``text`` for logging: verbatim when transcript logging is on,
    otherwise a placeholder that keeps only char/word counts.

    Accepts non-string values (callers log word lists and message objects);
    never raises.
    """
    if not isinstance(text, str):
        text = str(text)
    if transcript_logging_enabled():
        return text
    return f"<redacted: {len(text)} chars, {len(text.split())} words>"
