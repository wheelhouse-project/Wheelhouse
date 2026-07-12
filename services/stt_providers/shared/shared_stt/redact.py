"""Transcript redaction for provider-side log lines (wh-transcript-log-defaults).

Mirror of services/wheelhouse/utils/redact.py -- the app and the STT
providers are separate distributions that cannot share imports, so each
carries a copy of this small helper. The contract (env var name, "1"
semantics, placeholder format) must stay in sync with the app side.
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
