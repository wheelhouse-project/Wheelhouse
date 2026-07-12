"""wh-distil-hint-handler-parity: port the parakeet review fixes to the
distil add-hint and hard-restart handlers.

The parakeet hotwords review chain (wh-q33mj.1 through .4) fixed five
defects in these handler shapes. distil had the same shapes without the
fixes, and one worse: _handle_hard_restart_service stopped the service
even when the restart-flag write failed, and exit 0 with no flag reads
as a clean shutdown to the launcher (should_restart) -- STT dies
permanently from a voice command. The parakeet tests
(sherpa_offline_parakeet_stt_server tests/test_hotwords.py) are the
template for this file.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

import main as distil_main
from main import DistilMediumServer


@pytest.fixture
def flag_path(tmp_path, monkeypatch):
    """Point the launcher restart flag at a temp file so the real
    flag-write path runs without touching AppData."""
    import shared_stt.launcher as launcher_mod

    flag = tmp_path / "restart.flag"
    monkeypatch.setattr(
        launcher_mod, "get_restart_flag_path", lambda name: flag
    )
    return flag


@pytest.fixture
def server(flag_path, monkeypatch):
    """A DistilMediumServer shell: no audio, no engine, just the
    handler's collaborators. The add-hint gate re-reads config.toml, so
    the fixture patches load_config to report hotwords enabled; tests
    override it to flip the gate."""
    monkeypatch.setattr(
        distil_main,
        "load_config",
        lambda: {"hotwords": {"enabled": True}},
    )
    s = DistilMediumServer.__new__(DistilMediumServer)
    s.forwarder = MagicMock()
    s.hotwords_enabled = True
    s.stop = MagicMock()
    return s


@pytest.fixture
def hints_stub(monkeypatch):
    # The REAL hints_updater contract (wh-q33mj.1.2): add_hint returns
    # False for BOTH duplicate and I/O error (it catches Exception
    # internally), and get_hints returns [] on error. The stub mirrors
    # that; no test may stub add_hint raising.
    stub = types.ModuleType("hints_updater")
    stub.add_hint = MagicMock(return_value=True)
    stub.get_hints = MagicMock(return_value=["Zwicky"])
    monkeypatch.setitem(sys.modules, "hints_updater", stub)
    return stub


class TestHandleAddHint:
    def test_new_hint_added_and_restarts(self, server, hints_stub, flag_path):
        server._handle_add_hint("Zwicky")
        hints_stub.add_hint.assert_called_once_with("Zwicky")
        assert flag_path.read_text() == "restart"
        server.stop.assert_called_once()
        notification = server.forwarder.send_notification.call_args[0]
        assert "restarting" in notification[1].lower()

    def test_flag_failure_reports_deferred_not_restarting(
        self, server, hints_stub, monkeypatch
    ):
        # wh-q33mj.3.1: the "restarting to apply" announcement must come
        # AFTER the restart flag is durably written. Announcing first
        # and then failing the flag write sent two contradictory voice
        # notifications back to back.
        import shared_stt.launcher as launcher_mod

        def boom(name):
            raise OSError("AppData unwritable")

        monkeypatch.setattr(launcher_mod, "get_restart_flag_path", boom)
        server._handle_add_hint("Zwicky")
        server.stop.assert_not_called()
        messages = [
            c[0][1].lower()
            for c in server.forwarder.send_notification.call_args_list
        ]
        assert not any("restarting" in m for m in messages)
        assert "restart failed" in messages[-1]

    def test_hotwords_disabled_saves_without_restart(
        self, server, hints_stub, flag_path, monkeypatch
    ):
        # With [hotwords] enabled=false a restart reloads the model for
        # zero effect. Save the hint, skip the restart, and say so
        # honestly. The cached flag stays True to prove the gate reads
        # the config file, not the cache.
        monkeypatch.setattr(
            distil_main,
            "load_config",
            lambda: {"hotwords": {"enabled": False}},
        )
        server.hotwords_enabled = True
        server._handle_add_hint("Zwicky")
        hints_stub.add_hint.assert_called_once_with("Zwicky")
        server.stop.assert_not_called()
        assert not flag_path.exists()
        notification = server.forwarder.send_notification.call_args[0]
        assert "saved" in notification[1].lower()
        assert "restarting" not in notification[1].lower()

    def test_stale_disabled_cache_updates_from_config(
        self, server, hints_stub, flag_path
    ):
        # The user enables [hotwords] in config.toml AFTER the service
        # started. The gate must read the file fresh. Fixture
        # load_config says enabled.
        server.hotwords_enabled = False  # stale construction-time cache
        server._handle_add_hint("Zwicky")
        assert flag_path.read_text() == "restart"
        server.stop.assert_called_once()
        notification = server.forwarder.send_notification.call_args[0]
        assert "restarting" in notification[1].lower()

    def test_config_reread_failure_falls_back_to_cached(
        self, server, hints_stub, flag_path, monkeypatch
    ):
        # A malformed config.toml mid-edit must not turn add-hint into
        # an error; the gate falls back to the construction-time value.
        def boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(distil_main, "load_config", boom)
        server.hotwords_enabled = True
        server._handle_add_hint("Zwicky")
        assert flag_path.read_text() == "restart"
        server.stop.assert_called_once()

    def test_duplicate_hint_no_restart(self, server, hints_stub):
        # add_hint False + hint present in get_hints = duplicate.
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = ["Zwicky"]
        server._handle_add_hint("Zwicky")
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" in notification[1].lower()

    def test_duplicate_check_is_case_insensitive(self, server, hints_stub):
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = ["zwicky"]
        server._handle_add_hint("Zwicky")
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" in notification[1].lower()

    def test_long_duplicate_matches_truncated_stored_form(
        self, server, hints_stub
    ):
        # wh-q33mj.2.1: add_hint truncates hints over 100 chars before
        # storing and before its own duplicate check. The probe must
        # apply the same normalization, or a long duplicate is
        # misreported as a write failure.
        long_hint = "x" * 120
        stored_form = long_hint[:100]
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = [stored_form]
        server._handle_add_hint(long_hint)
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" in notification[1].lower()

    def test_write_error_reported_as_error_not_duplicate(
        self, server, hints_stub
    ):
        # wh-q33mj.1.2: add_hint False + hint ABSENT from get_hints =
        # the write failed (disk full, permissions). Telling the user
        # "already exists" loses the hint silently.
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = []
        server._handle_add_hint("Zwicky")
        server.stop.assert_not_called()
        notification = server.forwarder.send_notification.call_args[0]
        assert "already exists" not in notification[1].lower()
        assert "could not save" in notification[1].lower()


class TestHardRestartService:
    """wh-q33mj.1.3 parity: a failed restart-flag write must NOT stop
    the service -- exit 0 with no flag means the launcher never brings
    it back, permanently killing STT from a voice command."""

    @pytest.fixture
    def bare_server(self):
        s = DistilMediumServer.__new__(DistilMediumServer)
        s.forwarder = MagicMock()
        s.stop = MagicMock()
        return s

    def test_flag_written_then_stops(self, bare_server, tmp_path, monkeypatch):
        import shared_stt.launcher as launcher_mod

        flag = tmp_path / "restart.flag"
        monkeypatch.setattr(
            launcher_mod, "get_restart_flag_path", lambda name: flag
        )
        bare_server._handle_hard_restart_service()
        assert flag.read_text() == "restart"
        bare_server.stop.assert_called_once()

    def test_flag_write_failure_keeps_service_running(
        self, bare_server, monkeypatch
    ):
        import shared_stt.launcher as launcher_mod

        def boom(name):
            raise OSError("AppData unwritable")

        monkeypatch.setattr(launcher_mod, "get_restart_flag_path", boom)
        bare_server._handle_hard_restart_service()
        bare_server.stop.assert_not_called()
        notification = bare_server.forwarder.send_notification.call_args[0]
        assert "restart failed" in notification[1].lower()


class TestSoftRestartService:
    """Same defect as wh-parakeet-soft-restart-noop, distil copy: the
    soft-restart handler validates config.toml but applies nothing, so
    it must not claim the configuration was applied."""

    @pytest.fixture
    def soft_server(self, monkeypatch):
        monkeypatch.setattr(distil_main, "load_config", lambda: {})
        s = DistilMediumServer.__new__(DistilMediumServer)
        s.forwarder = MagicMock()
        return s

    def test_success_says_validated_not_applied(self, soft_server):
        soft_server._handle_restart_service()
        messages = [
            c[0][1].lower()
            for c in soft_server.forwarder.send_notification.call_args_list
        ]
        assert not any("reloaded successfully" in m for m in messages)
        assert any("restart to apply" in m for m in messages)

    def test_parse_failure_reports_error(self, soft_server, monkeypatch):
        def boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(distil_main, "load_config", boom)
        soft_server._handle_restart_service()
        messages = [
            c[0][1].lower()
            for c in soft_server.forwarder.send_notification.call_args_list
        ]
        assert any("failed" in m for m in messages)


class TestAddHintWriteFailureRedaction:
    """wh-797.19.3: the write-failure branch formatted the raw hint while
    the success and duplicate branches redact. The user-facing toast keeps
    the verbatim hint; only the log line is redacted."""

    def test_write_failure_log_redacts_hint_by_default(
        self, server, hints_stub, monkeypatch, caplog
    ):
        import logging

        monkeypatch.delenv("WHEELHOUSE_LOG_TRANSCRIPTS", raising=False)
        hints_stub.add_hint.return_value = False
        hints_stub.get_hints.return_value = []  # not stored -> write failed
        with caplog.at_level(logging.ERROR):
            server._handle_add_hint("propranolol dosage")

        assert "propranolol dosage" not in caplog.text
        assert "<redacted:" in caplog.text
