"""Transcript-logging release defaults, provider side (wh-transcript-log-defaults).

The STT provider processes inherit WHEELHOUSE_LOG_TRANSCRIPTS from
RemoteSTTLauncher's environment copy. shared_stt.redact mirrors the app-side
helper: only the exact value "1" enables full transcript logging, so a
provider launched standalone (env var absent) is privacy-safe by default.
"""
from __future__ import annotations

import pytest

from shared_stt.redact import redact_transcript, transcript_logging_enabled

ENV_VAR = "WHEELHOUSE_LOG_TRANSCRIPTS"


class TestTranscriptLoggingEnabled:
    def test_absent_env_var_means_disabled(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert transcript_logging_enabled() is False

    def test_env_var_1_means_enabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        assert transcript_logging_enabled() is True

    @pytest.mark.parametrize("value", ["0", "", "true"])
    def test_any_other_value_means_disabled(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert transcript_logging_enabled() is False


class TestRedactTranscript:
    def test_enabled_returns_text_unchanged(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        assert redact_transcript("dictated sentence here") == (
            "dictated sentence here"
        )

    def test_disabled_hides_content_keeps_lengths(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = redact_transcript("open the pod bay doors")  # 22 chars, 5 words
        assert "pod" not in out
        assert "22" in out
        assert "5" in out

    def test_disabled_non_string_is_coerced(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = redact_transcript(["alpha", "bravo"])
        assert "alpha" not in out
        assert isinstance(out, str)


class TestAddHintCommandLogRedaction:
    """wh-797.19.1: the ADD HINT command log carried the raw hint (the
    boost command's payload is user-selected text)."""

    def test_add_hint_log_redacts_hint_by_default(self, monkeypatch):
        import asyncio
        import json
        import threading

        from shared_stt.ws_forwarder import WSForwarder

        monkeypatch.delenv(ENV_VAR, raising=False)
        lines: list[str] = []
        forwarder = WSForwarder(
            host="localhost",
            port=59998,
            transcription_enabled_event=threading.Event(),
            add_hint_callback=lambda hint: None,
            log_func=lines.append,
        )

        class FakeWs:
            def __init__(self, messages):
                self._messages = list(messages)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._messages:
                    raise StopAsyncIteration
                return self._messages.pop(0)

        msg = json.dumps({"type": "add_hint", "hint": "propranolol dosage"})
        asyncio.run(forwarder._listen_for_commands(FakeWs([msg])))

        hint_lines = [line for line in lines if "ADD HINT" in line]
        assert hint_lines, "expected an ADD HINT command log line"
        joined = " ".join(hint_lines)
        assert "propranolol dosage" not in joined
        assert "<redacted:" in joined
