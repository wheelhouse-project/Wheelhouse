"""Transcript-logging release defaults, app side (wh-transcript-log-defaults).

Contract (release plan section 8): log lines that carry dictated text or
clipboard content are redacted by default. One documented config switch
(config.toml LOG_TRANSCRIPTS) re-enables full transcript logging; the
launcher propagates it to every child process as the environment variable
WHEELHOUSE_LOG_TRANSCRIPTS ("1" on, anything else / absent off), so the
privacy-safe state is the default for any process started outside the
launcher.
"""
from __future__ import annotations

import pytest

from utils.redact import redact_transcript, transcript_logging_enabled

ENV_VAR = "WHEELHOUSE_LOG_TRANSCRIPTS"


class TestTranscriptLoggingEnabled:
    def test_absent_env_var_means_disabled(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert transcript_logging_enabled() is False

    def test_env_var_1_means_enabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        assert transcript_logging_enabled() is True

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "on"])
    def test_any_other_value_means_disabled(self, monkeypatch, value):
        # Only the exact string "1" enables; the launcher writes "1"/"0".
        monkeypatch.setenv(ENV_VAR, value)
        assert transcript_logging_enabled() is False


class TestRedactTranscript:
    def test_enabled_returns_text_unchanged(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        assert redact_transcript("open the pod bay doors") == (
            "open the pod bay doors"
        )

    def test_disabled_hides_content(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = redact_transcript("my secret password")
        assert "secret" not in out
        assert "password" not in out

    def test_disabled_placeholder_carries_length_info(self, monkeypatch):
        # Diagnostic value without content: char and word counts survive.
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = redact_transcript("open the pod bay doors")  # 22 chars, 5 words
        assert "22" in out
        assert "5" in out

    def test_disabled_empty_string(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = redact_transcript("")
        assert "0" in out

    def test_disabled_non_string_is_coerced(self, monkeypatch):
        # Callers sometimes log objects (e.g. lists of words); never raise.
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = redact_transcript(["alpha", "bravo"])
        assert "alpha" not in out
        assert isinstance(out, str)


class TestLauncherPropagation:
    """launcher._read_transcript_logging_flag reads the single config key and
    never raises; launcher sets WHEELHOUSE_LOG_TRANSCRIPTS from it before
    spawning children."""

    def _read(self, tmp_path, body):
        import launcher

        cfg = tmp_path / "config.toml"
        if body is not None:
            cfg.write_text(body, encoding="utf-8")
        return launcher._read_transcript_logging_flag(cfg)

    def test_true_in_config(self, tmp_path):
        assert self._read(tmp_path, "LOG_TRANSCRIPTS = true\n") is True

    def test_false_in_config(self, tmp_path):
        assert self._read(tmp_path, "LOG_TRANSCRIPTS = false\n") is False

    def test_key_absent_defaults_false(self, tmp_path):
        assert self._read(tmp_path, "LOG_LEVEL = 'DEBUG'\n") is False

    def test_missing_file_defaults_false(self, tmp_path):
        assert self._read(tmp_path, None) is False

    def test_malformed_toml_defaults_false(self, tmp_path):
        assert self._read(tmp_path, "LOG_TRANSCRIPTS = = =\n") is False

    def test_non_bool_value_defaults_false(self, tmp_path):
        assert self._read(tmp_path, "LOG_TRANSCRIPTS = 'yes'\n") is False


class TestRedactContentFields:
    """websocket_manager._redact_content_fields (wh-797.17.3): payload
    dicts logged in broadcast()/send_command_to_stt() redact only the
    content-bearing keys; type/flags/levels stay verbatim so the payload
    remains diagnosable with redaction on."""

    def _fn(self):
        from integrations.websocket_manager import _redact_content_fields
        return _redact_content_fields

    def test_content_keys_redacted_metadata_verbatim(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        out = self._fn()({"type": "add_hint", "hint": "propranolol"})
        assert out["type"] == "add_hint"
        assert "propranolol" not in str(out)
        assert "redacted" in out["hint"]

    def test_content_free_payload_unchanged(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        payload = {"type": "set_log_level", "level": "DEBUG"}
        assert self._fn()(payload) == payload

    def test_passthrough_when_enabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        out = self._fn()({"type": "add_hint", "hint": "propranolol"})
        assert out["hint"] == "propranolol"

    def test_original_payload_not_mutated(self, monkeypatch):
        monkeypatch.delenv(ENV_VAR, raising=False)
        payload = {"type": "add_hint", "hint": "propranolol"}
        self._fn()(payload)
        assert payload["hint"] == "propranolol"


class TestSpeechNotifierLogRedaction:
    """wh-797.19.2: the 'Notification sent' debug line carried the raw
    toast body; provider notifications embed hint text (user-selected
    content via the boost command). The visible toast stays verbatim."""

    def test_notification_log_redacts_message_by_default(
        self, monkeypatch, caplog
    ):
        import logging

        monkeypatch.delenv(ENV_VAR, raising=False)
        import utils.speech_notifier as sn_mod

        sent = {}
        monkeypatch.setattr(
            sn_mod.notification, "notify", lambda **kw: sent.update(kw)
        )
        notifier = sn_mod.SpeechNotifier(enabled=True)
        with caplog.at_level(logging.DEBUG, logger="utils.speech_notifier"):
            notifier._send_notification("STT Hint", "added propranolol")

        assert sent["message"] == "added propranolol"
        assert "added propranolol" not in caplog.text
        assert "<redacted:" in caplog.text
