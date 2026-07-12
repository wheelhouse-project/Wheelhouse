"""direct_streamer._describe_response (wh-797.17.3).

The [g-stream-raw] debug line must keep the metadata the
log_stream_responses flag exists for (is_final, stability,
result_end_time) while redacting only the transcript text by default.
"""
from __future__ import annotations

from types import SimpleNamespace

from direct_streamer import _describe_response

ENV_VAR = "WHEELHOUSE_LOG_TRANSCRIPTS"


def _resp(transcript="hello world", is_final=False, stability=0.87):
    alt = SimpleNamespace(transcript=transcript)
    result = SimpleNamespace(
        is_final=is_final,
        stability=stability,
        result_end_time="1.5s",
        alternatives=[alt],
    )
    return SimpleNamespace(results=[result])


class TestDescribeResponse:
    def test_metadata_kept_transcript_redacted_by_default(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = _describe_response(_resp())
        assert "is_final=False" in out
        assert "stability=0.87" in out
        assert "1.5s" in out
        assert "hello world" not in out
        assert "redacted" in out

    def test_transcript_verbatim_when_enabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        out = _describe_response(_resp())
        assert "hello world" in out

    def test_empty_results(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = _describe_response(SimpleNamespace(results=[]))
        assert out == "results=[]"

    def test_result_without_alternatives(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        result = SimpleNamespace(
            is_final=True, stability=0.0, result_end_time="0s", alternatives=[]
        )
        out = _describe_response(SimpleNamespace(results=[result]))
        assert "is_final=True" in out
